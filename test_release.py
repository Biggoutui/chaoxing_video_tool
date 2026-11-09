"""发布相关回归测试：只使用临时文件和模拟浏览器，不访问真实课程。"""
import argparse
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import video_tool


URL = ('https://mooc1.chaoxing.com/mycourse/studentstudy?'
       'courseId=demo-course&clazzid=demo-class&cpi=demo-user&chapterId=1')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(video_tool, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.root_patch.stop)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding='utf-8')

    def test_url_argument_works_without_config(self):
        self.assertEqual(video_tool.load_settings(URL), (URL, 180))

    def test_missing_url_has_setup_instructions(self):
        with self.assertRaisesRegex(ValueError, 'config.example.json.*--url'):
            video_tool.load_settings()

    def test_invalid_config_json_has_filename(self):
        (self.root / 'config.json').write_text('{bad', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'config.json.*JSON'):
            video_tool.load_settings()

    def test_config_types_and_timeout_are_validated(self):
        for config in [[], {'url': 123}, {'url': URL, 'stall_timeout': True},
                       {'url': URL, 'stall_timeout': 0},
                       {'url': URL, 'stall_timeout': -1},
                       {'url': URL, 'stall_timeout': '180'},
                       {'url': URL, 'stall_timeout': float('inf')},
                       {'url': URL, 'stall_timeout': float('nan')}]:
            with self.subTest(config=config):
                self.write('config.json', config)
                with self.assertRaises(ValueError):
                    video_tool.load_settings()

    def test_explicit_url_overrides_progress_and_config(self):
        self.write('config.json', {'url': URL, 'stall_timeout': 90})
        self.write('progress.json', {'url': URL.replace('chapterId=1', 'chapterId=2')})
        explicit = URL.replace('chapterId=1', 'chapterId=3')
        self.assertEqual(video_tool.load_settings(explicit), (explicit, 90))

    def test_matching_progress_is_used(self):
        self.write('config.json', {'url': URL})
        saved = URL.replace('chapterId=1', 'chapterId=2')
        self.write('progress.json', {'url': saved})
        self.assertEqual(video_tool.load_settings()[0], saved)

    def test_other_course_class_or_user_does_not_resume(self):
        self.write('config.json', {'url': URL})
        for old, new in [('demo-course', 'other-course'), ('demo-class', 'other-class'),
                         ('demo-user', 'other-user')]:
            with self.subTest(identity=old):
                self.write('progress.json', {'url': URL.replace(old, new)})
                self.assertEqual(video_tool.load_settings()[0], URL)

    def test_incomplete_or_ambiguous_identity_does_not_resume(self):
        for address in [URL.replace('&cpi=demo-user', ''),
                        URL + '&cpi=another-user',
                        URL + '&userId=another-user']:
            self.write('config.json', {'url': address})
            saved = address.replace('chapterId=1', 'chapterId=2')
            if 'userId=' in address:
                saved = saved.replace('another-user', 'changed-user')
            self.write('progress.json', {'url': saved})
            self.assertEqual(video_tool.load_settings()[0], address)

    def test_broken_progress_falls_back_without_printing_contents(self):
        self.write('config.json', {'url': URL})
        for contents in ['{secret-invalid-json', '[]', '{"url":123}',
                         '{"url":"https://example.com/?secret=private"}']:
            with self.subTest(contents=contents):
                (self.root / 'progress.json').write_text(contents, encoding='utf-8')
                with self.assertLogs(video_tool.LOG, level='WARNING') as logs:
                    self.assertEqual(video_tool.load_settings()[0], URL)
                self.assertNotIn('private', str(logs.output))
                self.assertNotIn('secret-invalid-json', str(logs.output))

    def test_url_types_and_lookalike_hosts_are_rejected(self):
        for address in [None, 1, [],
                        'https://chaoxing.com.evil.test/mycourse/studentstudy',
                        'https://fakechaoxing.com/mycourse/studentstudy']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                video_tool.validate_url(address)

    def test_domain_helper_does_not_accept_substring_matches(self):
        self.assertTrue(video_tool.is_chaoxing_host('mooc1.chaoxing.com'))
        self.assertTrue(video_tool.is_chaoxing_host('chaoxing.com'))
        self.assertFalse(video_tool.is_chaoxing_host('chaoxing.com.evil.test'))
        self.assertFalse(video_tool.is_chaoxing_host('fakechaoxing.com'))
        self.assertFalse(video_tool.is_chaoxing_host(None))

    def test_log_formatter_redacts_urls_in_messages_and_exceptions(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(video_tool.PrivateFormatter('%(message)s'))
        logger = logging.Logger('privacy-test')
        logger.addHandler(handler)
        address = 'https://name:password@mooc1.chaoxing.com/path?enc=private&cpi=user#fragment'
        logger.warning('Browser error: %s', address)
        try:
            raise ValueError(address)
        except ValueError:
            logger.exception('failed')
        text = output.getvalue()
        self.assertIn('mooc1.chaoxing.com/path', text)
        for secret in ['name:', 'password', 'private', 'cpi=user', 'fragment']:
            self.assertNotIn(secret, text)

    def test_human_pause_does_not_capture_by_default(self):
        with self.assertLogs(video_tool.LOG, level='WARNING'):
            with patch('builtins.input', return_value=''), \
                    patch.object(video_tool, 'dump_diagnostics') as dump:
                video_tool.human_pause(MagicMock(), 'test')
        dump.assert_not_called()

    def test_human_pause_can_explicitly_capture(self):
        with self.assertLogs(video_tool.LOG, level='WARNING'):
            with patch('builtins.input', return_value=''), \
                    patch.object(video_tool, 'dump_diagnostics') as dump:
                video_tool.human_pause(MagicMock(), 'test', diagnostics=True)
        dump.assert_called_once()

    def test_normal_run_and_browser_errors_do_not_capture_by_default(self):
        self.write('config.json', {'url': URL})
        args = argparse.Namespace(url=None, login=False, inspect=False,
                                  once=False, diagnostics=False)
        for fail in [False, True]:
            with self.subTest(browser_failure=fail):
                with patch.object(video_tool, 'sync_playwright') as pw, \
                        patch.object(video_tool, 'wait_for_course') as wait, \
                        patch.object(video_tool, 'save_position'), \
                        patch.object(video_tool, 'chapter_id', side_effect=['1', '2']), \
                        patch.object(video_tool, 'watch_chapter', return_value=True), \
                        patch.object(video_tool, 'advance_chapter', side_effect=[True, False]), \
                        patch.object(video_tool, 'dump_diagnostics') as dump:
                    context = pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    page = context.pages[0]
                    page.is_closed.return_value = False
                    wait.return_value = page
                    if fail:
                        page.goto.side_effect = video_tool.Error('simulated browser error')
                        with self.assertRaises(video_tool.Error):
                            video_tool.run(args)
                    else:
                        video_tool.run(args)
                    dump.assert_not_called()

    def test_inspect_and_explicit_error_diagnostics_capture(self):
        self.write('config.json', {'url': URL})
        for inspect in [False, True]:
            args = argparse.Namespace(url=None, login=False, inspect=inspect,
                                      once=False, diagnostics=not inspect)
            with self.subTest(inspect=inspect):
                with patch.object(video_tool, 'sync_playwright') as pw, \
                        patch.object(video_tool, 'wait_for_course') as wait, \
                        patch.object(video_tool, 'save_position'), \
                        patch('builtins.input', return_value=''), \
                        patch.object(video_tool, 'dump_diagnostics') as dump:
                    context = pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    page = context.pages[0]
                    wait.return_value = page
                    if inspect:
                        video_tool.run(args)
                    else:
                        page.goto.side_effect = video_tool.Error('simulated browser error')
                        with self.assertRaises(video_tool.Error):
                            video_tool.run(args)
                    dump.assert_called_once()

    def test_selectors_file_requires_json_object(self):
        self.write('quiz_selectors.json', [])
        with self.assertRaisesRegex(ValueError, 'quiz_selectors.json'):
            video_tool.read_json_object('quiz_selectors.json')

    def test_log_formatter_redacts_windows_paths(self):
        formatter = video_tool.PrivateFormatter('%(message)s')
        record = logging.LogRecord('test', logging.ERROR, '', 0,
                                   'File missing: %s', (r'C:\Users\Example User\config.json',), None)
        self.assertEqual(formatter.format(record), 'File missing: [LOCAL_PATH]')

    def test_first_run_error_is_utf8_even_with_legacy_console_encoding(self):
        script = self.root / 'video_tool.py'
        script.write_bytes(Path(video_tool.__file__).read_bytes())
        env = dict(os.environ, PYTHONIOENCODING='gbk')
        result = subprocess.run([sys.executable, '-B', str(script)],
                                cwd=self.root, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        message = result.stderr.decode('utf-8')
        self.assertIn('config.example.json', message)
        self.assertIn('--url', message)
        self.assertFalse((self.root / 'browser_profile').exists())


if __name__ == '__main__':
    unittest.main()
