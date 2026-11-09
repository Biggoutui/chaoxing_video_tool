"""在本地模拟网页验证真实 Chrome DOM 操作，不连接课程网站。"""
import json
import unittest
from pathlib import Path
import tempfile
import video_tool

from playwright.sync_api import sync_playwright

from video_tool import (QuizHandler, advance_chapter, chapter_id, content_frame,
                        next_chapter, task_completed, video_frames, watch_chapter)

BASE = 'https://fixture.chaoxing.com'

MAIN = '''<li id="dct1" title="视频" class="active">视频</li>
<li id="dct2" title="章节测验" onclick="window.assignmentClicks++">章节测验</li>
<div id="cur1" class="posCatalog_active"><span class="posCatalog_name">一</span></div>
<div id="cur2"><span class="posCatalog_name" onclick="go()">二</span></div>
<button id="prevNextFocusNext" onclick="window.assignmentClicks++">下一节</button>
<iframe id="iframe" src="/cards?knowledgeid=1"></iframe>
<script>window.assignmentClicks=0; function go(){
document.querySelector('#cur1').classList.remove('posCatalog_active');
document.querySelector('#cur2').classList.add('posCatalog_active');
document.querySelector('#iframe').src='/cards?knowledgeid=2';
}</script>'''

CARD = '''<div class="ans-attach-ct"><div class="ans-job-icon" aria-label="任务点未完成"></div>
<iframe src="/ananas/modules/video/index.html"></iframe></div>'''

PLAYER = '''<video></video><button onclick="start()">播放视频</button>
<script>
const state={current:0,duration:3,ended:false,paused:true,ready:0};
const video=document.querySelector('video');
for(const [key,prop] of Object.entries({currentTime:'current',duration:'duration',
ended:'ended',paused:'paused',readyState:'ready'}))
Object.defineProperty(video,key,{get:()=>state[prop],configurable:true});
function start(){state.paused=false;state.ready=4;
document.querySelector('button').hidden=true;
setTimeout(()=>{state.current=3;state.ended=true;state.paused=true},150);
setTimeout(()=>window.frameElement.closest('.ans-attach-ct').querySelector('.ans-job-icon').setAttribute('aria-label','任务点已完成'),400);
}
</script>'''

QUIZ = '''<div id="quiz"><h3>单选题：测试</h3>
<label><input type="radio" name="q">甲</label><label><input type="radio" name="q">乙</label>
<p id="failure" hidden>回答错误，请重新选择</p>
<button id="submit" onclick="submit()">提交</button></div>
<script>window.submissions=0; function submit(){ window.submissions++;
if(document.querySelectorAll('input')[1].checked) document.querySelector('#quiz').hidden=true;
else document.querySelector('#failure').hidden=false;
}</script>'''

SELECTORS = dict(container='#quiz', options='input', title='h3', submit='#submit', failure='#failure')


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = video_tool.ROOT
        video_tool.ROOT = Path(self.temp.name)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.documents = {'/mycourse/studentstudy': MAIN, '/cards': CARD,
                          '/ananas/modules/video/index.html': PLAYER}
        from urllib.parse import urlsplit

        def route(request):
            path = urlsplit(request.request.url).path
            request.fulfill(status=200, content_type='text/html; charset=utf-8',
                            body=self.documents.get(path, ''))

        self.context.route(BASE + '/**', route)
        self.page.goto(BASE + '/mycourse/studentstudy?chapterId=1')
        self.page.wait_for_load_state('load')

    def tearDown(self):
        self.context.close()
        video_tool.ROOT = self.old_root
        self.temp.cleanup()

    def test_nested_video_and_task_match(self):
        frames = video_frames(self.page)
        self.assertEqual(len(frames), 1)
        self.assertIs(task_completed(frames[0]), False)
        self.assertEqual(chapter_id(self.page), '1')

    def test_resume_wait_for_confirm_then_finish_without_homework(self):
        self.assertTrue(watch_chapter(self.page, QuizHandler(), timeout=3, poll=.1))
        self.assertIs(task_completed(video_frames(self.page)[0]), True)
        self.assertEqual(self.page.evaluate('window.assignmentClicks'), 0)
        self.assertFalse((Path(video_tool.ROOT) / 'diagnostics').exists())

    def test_directory_advance_skips_assignment_card(self):
        self.assertEqual(next_chapter(self.page), 'cur2')
        self.assertTrue(advance_chapter(self.page))
        self.assertEqual(chapter_id(self.page), '2')
        self.assertIn('knowledgeid=2', content_frame(self.page).url)
        self.assertEqual(self.page.evaluate('window.assignmentClicks'), 0)
        self.assertFalse(advance_chapter(self.page))

    def test_retry_single_choice_stops_on_success(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ)
        handler = QuizHandler(SELECTORS)
        self.assertTrue(handler.handle(frame))
        self.assertTrue(handler.handle(frame))
        self.assertFalse(handler.handle(frame))
        self.assertEqual(frame.evaluate('window.submissions'), 2)


    def test_no_explicit_failure_does_not_submit_twice(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ.replace('document.querySelector(\'#failure\').hidden=false', 'void(0)'))
        handler = QuizHandler(SELECTORS)
        self.assertTrue(handler.handle(frame))
        with self.assertRaisesRegex(ValueError, '答错反馈'):
            handler.handle(frame)
        self.assertEqual(frame.evaluate('window.submissions'), 1)

    def test_unknown_popup_stops_without_answering(self):
        frame = video_frames(self.page)[0]
        frame.locator('body').evaluate('(e, html) => e.insertAdjacentHTML("beforeend", html)', QUIZ.replace('>提交<', '>未知操作<'))
        with self.assertRaisesRegex(ValueError, '尚未适配'):
            watch_chapter(self.page, QuizHandler(), timeout=1, poll=.1)
        self.assertEqual(frame.locator('input:checked').count(), 0)

    def test_late_second_video_is_not_skipped(self):
        card = content_frame(self.page)
        card.evaluate('''() => setTimeout(() => {
            const div=document.createElement('div'); div.className='ans-attach-ct';
            div.innerHTML='<div class="ans-job-icon" aria-label="任务点未完成"></div><iframe src="/ananas/modules/video/index.html?second=1"></iframe>';
            document.body.append(div);
        }, 200)''')
        self.assertTrue(watch_chapter(self.page, QuizHandler(), timeout=3, poll=.1))
        frames = video_frames(self.page)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(task_completed(f) is True for f in frames))

    def test_video_tab_arriving_after_assignment_tab_is_not_skipped(self):
        self.page.evaluate('''() => {
            document.querySelector('#dct1').remove();
            setTimeout(()=>{
                const tab=document.createElement('li');tab.id='dct1';tab.title='视频';tab.className='active';
                document.body.prepend(tab);
            },3500);
        }''')
        self.assertTrue(watch_chapter(self.page, QuizHandler(), timeout=3, poll=.1))
        self.assertIs(task_completed(video_frames(self.page)[0]), True)

    def test_failure_without_retry_permission_does_not_retry(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ.replace('回答错误，请重新选择', '回答错误'))
        handler = QuizHandler(SELECTORS)
        handler.handle(frame)
        with self.assertRaisesRegex(ValueError, '允许重试'):
            handler.handle(frame)
        self.assertEqual(frame.evaluate('window.submissions'), 1)

    def test_scoring_or_limited_attempts_require_manual_answer(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ.replace('单选题：测试', '单选题：剩余1次'))
        with self.assertRaisesRegex(ValueError, '次数限制'):
            QuizHandler(SELECTORS).handle(frame)
        self.assertEqual(frame.evaluate('window.submissions'), 0)

    def test_standard_choice_popup_can_be_discovered_without_css_config(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ)
        handler = QuizHandler()
        self.assertTrue(handler.handle(frame))
        self.assertTrue(handler.handle(frame))
        self.assertFalse(handler.handle(frame))
        self.assertEqual(frame.evaluate('window.submissions'), 2)

    def test_discovery_does_not_use_buttons_outside_popup(self):
        frame = video_frames(self.page)[0]
        frame.set_content('<input type="radio"><button>开始播放</button>')
        self.assertFalse(QuizHandler().handle(frame))

    def test_new_chapter_must_not_reuse_prior_quiz_attempts(self):
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ)
        handler = QuizHandler(SELECTORS)
        handler.handle(frame)
        handler.handle(frame)
        self.assertTrue(advance_chapter(self.page))
        self.page.frame_locator('#iframe').frame_locator('iframe').locator('video').wait_for()
        frame = video_frames(self.page)[0]
        frame.set_content(QUIZ)
        handler.handle(frame)
        self.assertTrue(frame.locator('input').nth(0).is_checked())

    def test_real_chaoxing_markup_continues_after_correct_counter_changes(self):
        frame = video_frames(self.page)[0]
        html = (Path(__file__).parent / 'fixtures' / 'chaoxing_quiz.html').read_text(encoding='utf-8')
        frame.set_content(html + '''<style>#videoquiz-continue,#spanNot,#spanNotBack,#spanNotBackPoint{display:none}</style><script>
            window.submissions=0;window.continued=false;
            document.querySelector('#videoquiz-submit').onclick=()=>{
                window.submissions++;
                if(document.querySelectorAll('input')[1].checked){
                    document.querySelector('#rightNum').innerText='1';
                    document.querySelector('#videoquiz-submit').style.display='none';
                    document.querySelector('#videoquiz-continue').style.display='block';
                } else document.querySelector('#spanNotBackPoint').style.display='block';
            };
            document.querySelector('#videoquiz-continue').onclick=()=>{
                window.continued=true;document.querySelector('.ans-videoquiz').hidden=true;
            };
        </script>''')
        handler=QuizHandler()
        self.assertTrue(handler.handle(frame))
        self.assertTrue(handler.handle(frame))
        self.assertTrue(handler.handle(frame))
        self.assertTrue(frame.evaluate('window.continued'))
        self.assertEqual(frame.evaluate('window.submissions'), 2)
        self.assertFalse((Path(video_tool.ROOT) / 'diagnostics').exists())


if __name__ == '__main__':
    unittest.main()
