import unittest

from video_tool import action_for, answer_candidates, validate_url


class DecisionTests(unittest.TestCase):
    def test_playing_is_observed(self):
        self.assertEqual(action_for(dict(ended=False, paused=False, ready=4), False), 'wait')

    def test_paused_can_resume(self):
        self.assertEqual(action_for(dict(ended=False, paused=True, ready=4), False), 'play')

    def test_buffering_is_not_repeatedly_clicked(self):
        self.assertEqual(action_for(dict(ended=False, paused=True, ready=1), False), 'wait')

    def test_end_requires_task_confirmation(self):
        self.assertEqual(action_for(dict(ended=True, paused=True, ready=4), False), 'confirm')

    def test_confirmed_end_advances(self):
        self.assertEqual(action_for(dict(ended=True, paused=True, ready=4), True), 'done')

    def test_task_completion_alone_does_not_truncate_video(self):
        self.assertEqual(action_for(dict(ended=False, paused=False, ready=4), True), 'wait')

    def test_single_choice_is_bounded(self):
        self.assertEqual(answer_candidates('single', 3), [(0,), (1,), (2,)])

    def test_multiple_choice_combinations_are_bounded(self):
        self.assertEqual(answer_candidates('multiple', 2), [(0,), (1,), (0, 1)])
        with self.assertRaises(ValueError):
            answer_candidates('multiple', 8)

    def test_unknown_type_does_not_submit(self):
        with self.assertRaises(ValueError):
            answer_candidates('text', 4)

    def test_only_course_urls_are_accepted(self):
        self.assertEqual(validate_url('https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1'),
                         'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1')
        for url in ['https://example.com/', 'javascript:alert(1)', 'https://mooc1.chaoxing.com.evil.org/mycourse/studentstudy']:
            with self.assertRaises(ValueError):
                validate_url(url)


if __name__ == '__main__':
    unittest.main()
