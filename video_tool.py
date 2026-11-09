"""超星视频播放助手：只操作视频卡片，不提交章节测验。"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('video_tool')


class PrivateFormatter(logging.Formatter):
    """对最终日志文本脱敏，也覆盖 Playwright 错误和异常堆栈。"""
    def format(self, record):
        text = super().format(record)

        def redact(match):
            try:
                parts = urlsplit(match.group())
                host = parts.netloc.rsplit('@', 1)[-1]
                return urlunsplit((parts.scheme, host, parts.path,
                                   '[REDACTED]' if parts.query else '', ''))
            except ValueError:
                return '[REDACTED_URL]'

        text = re.sub(r'https?://[^\s<>"\x27]+', redact, text, flags=re.IGNORECASE)
        return re.sub(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n"\x27<>]*',
                      '[LOCAL_PATH]', text)


def is_chaoxing_host(host):
    host = (host or '').lower()
    return host == 'chaoxing.com' or host.endswith('.chaoxing.com')


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError('课程地址必须是字符串。')
    parts = urlsplit(url.strip())
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or not is_chaoxing_host(parts.hostname)
            or 'studentstudy' not in parts.path):
        raise ValueError('请使用超星课程 studentstudy 学习页的 HTTPS 地址。')
    return url.strip()


def read_json_object(name):
    try:
        value = json.loads((ROOT / name).read_text(encoding='utf-8-sig'))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError(f'{name} 不是有效的 UTF-8 JSON 文件，请检查格式。') from None
    if not isinstance(value, dict):
        raise ValueError(f'{name} 的顶层必须是 JSON 对象。')
    return value


def same_course_identity(first, second):
    """只有课程、班级和用户标识齐全且一致时，才复用历史地址。"""
    first_parts, second_parts = urlsplit(first), urlsplit(second)
    if (first_parts.hostname, first_parts.path) != (second_parts.hostname, second_parts.path):
        return False
    keys = ('courseId', 'clazzid', 'cpi', 'uid', 'userId', 'puid')

    def identity(parts):
        query = parse_qs(parts.query, keep_blank_values=True)
        selected = {key: query[key] for key in keys if key in query}
        if not all(key in selected for key in ('courseId', 'clazzid')):
            return None
        if not any(key in selected for key in keys[2:]):
            return None
        if any(len(values) != 1 or not values[0].strip() for values in selected.values()):
            return None
        return selected

    current = identity(first_parts)
    return current is not None and current == identity(second_parts)


def load_settings(explicit_url=None):
    config = read_json_object('config.json') if (ROOT / 'config.json').exists() else {}
    configured_url = config.get('url', '')
    if not isinstance(configured_url, str):
        raise ValueError('config.json 的 url 必须是字符串。')
    timeout = config.get('stall_timeout', 180)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('config.json 的 stall_timeout 必须是大于 0 的有限数字（秒）。')
    if explicit_url is not None:
        return validate_url(explicit_url), timeout
    if not configured_url.strip():
        raise ValueError('请复制 config.example.json 为 config.json 并填写 url，或使用 --url 指定学习页地址。')
    url = validate_url(configured_url)
    if (ROOT / 'progress.json').exists():
        try:
            saved = validate_url(read_json_object('progress.json').get('url'))
            if same_course_identity(url, saved):
                url = saved
        except (ValueError, OSError):
            LOG.warning('progress.json 无法读取或格式无效，已忽略历史进度，使用配置地址。')
    return url, timeout


def action_for(state, completed):
    if state.get('ended'):
        return 'done' if completed else 'confirm'
    if state.get('paused') and state.get('ready', 0) >= 2:
        return 'play'
    return 'wait'


def answer_candidates(kind, count, limit=31):
    if kind not in ('single', 'multiple') or not 1 <= count <= 10:
        raise ValueError('题型或选项数量不受支持，请手动处理。')
    total = count if kind == 'single' else 2 ** count - 1
    if total > limit:
        raise ValueError('选项组合过多，请手动处理，避免长时间重复提交。')
    sizes = (1,) if kind == 'single' else range(1, count + 1)
    return [choice for size in sizes for choice in itertools.combinations(range(count), size)]


def chapter_id(page):
    active = page.locator('.posCatalog_active[id^="cur"]')
    if active.count() == 1:
        return active.get_attribute('id')[3:]
    return parse_qs(urlsplit(page.url).query).get('chapterId', [''])[0]


def next_chapter(page):
    """目录顺序优先，禁止使用会进入作业卡片的“下一节”按钮。"""
    return page.locator('[id^="cur"]').evaluate_all('''elements => {
        const active = elements.findIndex(e => e.classList.contains('posCatalog_active'));
        if (active < 0) throw new Error('找不到目录中当前章节');
        for (const element of elements.slice(active + 1)) {
            if (element.querySelector('.posCatalog_name')) return element.id;
        }
        return null;
    }''')


def save_position(page, base_url):
    current = chapter_id(page)
    if not current:
        return
    parts = urlsplit(validate_url(base_url))
    query = parse_qs(parts.query, keep_blank_values=True)
    query['chapterId'] = [current]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ''))
    target = ROOT / 'progress.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps({'url': url, 'chapter': current}, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)


def content_frame(page):
    element = page.query_selector('#iframe')
    return element.content_frame() if element else None


def video_frames(page):
    card = content_frame(page)
    if not card:
        return []
    result = []
    for frame in card.child_frames:
        if frame.is_detached():
            continue
        # 只在视频播放器框架操作，绝不扫描章节作业的输入框。
        if '/ananas/modules/video/' in frame.url and frame.locator('video').count():
            result.append(frame)
    return result


def video_state(frame):
    return frame.locator('video').first.evaluate('''v => ({
        current: v.currentTime, duration: Number.isFinite(v.duration) ? v.duration : 0,
        paused: v.paused, ended: v.ended, ready: v.readyState,
        error: v.error ? v.error.code : null
    })''')


def task_completed(frame):
    """读取此播放器对应的任务点，不使用整章绿勾或其它视频的任务点。"""
    owner = frame.frame_element()
    return owner.evaluate('''e => {
        const container = e.closest('.ans-attach-ct');
        const job = container && container.querySelector('.ans-job-icon');
        if (!job) return null;
        return job.getAttribute('aria-label') === '任务点已完成';
    }''')


def click_play(frame):
    for name in ('播放视频', '播放', 'Play Video', 'Play'):
        button = frame.get_by_role('button', name=name, exact=True)
        if button.count() and button.first.is_visible():
            button.first.click(timeout=4000)
            return True
    return False


def dump_diagnostics(page, reason):
    LOG.warning('正在保存原始截图和 HTML，可能包含个人信息；请勿直接上传或发送整个诊断目录。')
    stamp = time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns() % 1000000000:09d}'
    folder = ROOT / 'diagnostics' / stamp
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'reason.txt').write_text(reason, encoding='utf-8')
    try:
        page.screenshot(path=str(folder / 'page.png'), timeout=10000)
        for index, frame in enumerate(video_frames(page)):
            # 只保存视频区域的 DOM，不保存整站表单、Cookie、localStorage。
            (folder / f'video-{index}.html').write_text(frame.locator('body').inner_html(), encoding='utf-8')
        card = content_frame(page)
        if card:
            jobs = card.locator('.ans-job-icon').evaluate_all(
                "es => es.map(e => ({className:e.className,label:e.getAttribute('aria-label')}))")
            (folder / 'tasks.json').write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding='utf-8')
    except Error as exc:
        LOG.warning('部分诊断保存失败：%s', type(exc).__name__)
    LOG.info('诊断：%s；保存目录：diagnostics/%s', reason, stamp)
    return folder


def human_pause(page, reason, *, diagnostics=False):
    LOG.warning('需要人工处理：%s', reason)
    if diagnostics:
        dump_diagnostics(page, reason)
    try:
        import winsound
        winsound.MessageBeep()
    except (ImportError, RuntimeError):
        pass
    try:
        input('\n请在浏览器处理上述问题，完成后回到此窗口按回车继续；Ctrl+C 退出：')
    except EOFError as exc:
        raise ValueError('当前运行没有交互终端；请通过 start.cmd 启动以进行人工恢复。') from exc


class QuizHandler:
    """弹题适配由 quiz_selectors.json 定义，未知 DOM 停止并提示人工。"""
    def __init__(self, selectors=None):
        self.selectors = selectors or {}
        self.attempts = {}
        self.discovered = {}

    def discover(self, frame):
        """仅识别播放器内含标准选择框及明确提交按钮的最小弹窗。"""
        return frame.locator('body').evaluate(r'''body => {
            const visible = e => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
            const inputs = Array.from(body.querySelectorAll('input[type=radio],input[type=checkbox]')).filter(visible);
            if (!inputs.length) return null;
            const path = (e, stop=body) => {
                if(e.id) return '#' + CSS.escape(e.id);
                const parts=[];
                for(let n=e; n && n!==stop; n=n.parentElement){
                    const siblings=Array.from(n.parentElement.children).filter(x=>x.tagName===n.tagName);
                    parts.unshift(n.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(n)+1)+')');
                }
                return (stop===body?'body > ':':scope > ')+parts.join(' > ');
            };
            const buttons = e => Array.from(e.querySelectorAll('button,a,input[type=button],input[type=submit],[role=button]'));
            const label = e => (e.innerText || e.value || '').trim();
            let root=inputs[0].parentElement;
            while(root && root!==body){
                const submit=buttons(root).filter(e=>visible(e) && /^(提交|提交答案|确定|确认|确认答案)$/.test(label(e)));
                if(submit.length===1 && inputs.every(e=>root.contains(e))){
                    const descendants=Array.from(root.querySelectorAll('p,span,div'));
                    const failure=descendants.find(e=>e.children.length===0 && /回答错误|答案错误|回答不正确|答错|请重新选择|请重新作答|回答有误/.test(e.textContent));
                    const success=descendants.find(e=>e.children.length===0 && /回答正确|回答成功|答对了/.test(e.textContent));
                    const continuation=buttons(root).find(e=>/^(继续播放|继续学习|继续|关闭)$/.test(label(e)));
                    const title=root.querySelector('h1,h2,h3,h4,h5,h6');
                    return {container:path(root), options:'input[type=radio],input[type=checkbox]',
                        submit:path(submit[0],root), title:title?path(title,root):null,
                        failure:failure?path(failure,root):null,
                        success:success?path(success,root):null,continue:continuation?path(continuation,root):null};
                }
                root=root.parentElement;
            }
            return null;
        }''')

    def handle(self, frame):
        scope = (frame.parent_frame.url if frame.parent_frame else '', frame.url)
        config = self.selectors
        real_quiz = frame.locator('.ans-videoquiz:visible')
        if not config and real_quiz.count():
            config = dict(container='.ans-videoquiz', options='.ans-videoquiz-opt input',
                          title='.tkItem_title', submit='#videoquiz-submit',
                          failure='#spanNotBackPoint:visible, #spanNot:visible, #spanNotBack:visible', correct_count='#rightNum',
                          continuation='#videoquiz-continue')
        if not config:
            found = self.discover(frame)
            if found:
                self.discovered[scope] = found
            config = found or self.discovered.get(scope, {})
        if not config.get('container'):
            return False
        boxes = frame.locator(config['container'])
        visible = [box for box in boxes.all() if box.is_visible()]
        if not visible:
            return False
        if len(visible) != 1:
            raise ValueError('同时出现多个弹题容器，需要人工检查。')
        box = visible[0]
        text = box.inner_text()
        if config.get('correct_count'):
            questions = box.locator('.tkItem').count()
            if questions != 1:
                raise ValueError('本次弹窗包含多道题，需要人工确认结构后适配。')
            counter = box.locator(config['correct_count'])
            correct = counter.inner_text().strip() if counter.count() else ''
            continuation = box.locator(config['continuation'])
            if correct.isdigit() and int(correct) >= questions and continuation.is_visible():
                continuation.click(timeout=4000)
                LOG.info('视频弹题已答对，已点击继续学习。')
                frame.page.wait_for_timeout(800)
                return True
        # 存在次数限制或评分提示时不使用无答案尝试模式。
        if re.search(r'扣分|剩余.{0,6}次|仅.{0,3}次|最多.{0,3}次|成绩|计分', text):
            raise ValueError('弹题显示评分或次数限制，请手动回答。')
        if config.get('success') and config.get('continue'):
            success = box.locator(config['success'])
            if success.count() and success.first.is_visible():
                continuation = box.locator(config['continue'])
                continuation.click(timeout=4000)
                return True
        options = box.locator(config['options'])
        count = options.count()
        if not count or any(not option.is_visible() for option in options.all()):
            raise ValueError('弹题选项结构变化，需要人工检查。')
        kind = 'multiple' if '多选' in text else 'single'
        if '填空' in text or '简答' in text:
            raise ValueError('当前弹题不是选择题，请手动回答。')
        title = box.locator(config['title']).inner_text() if config.get('title') else text.splitlines()[0]
        identity = (scope, title, tuple(options.all_text_contents()))
        record = self.attempts.setdefault(identity, {'index': 0, 'pending': False})
        if record['pending']:
            if config.get('correct_count'):
                # 本站实际弹题：仍遮挡视频且提交按钮恢复可用，按用户要求尝试下一项。
                submit = box.locator(config['submit'])
                if not submit.is_visible() or not submit.is_enabled():
                    return True  # 等待提交中状态结束，不重复提交。
                if any(not option.is_enabled() for option in options.all()):
                    raise ValueError('弹题选项不可再选择，请手动处理。')
            else:
                failure = box.locator(config['failure']) if config.get('failure') else None
                if failure is None or not failure.count() or not failure.first.is_visible():
                    raise ValueError('提交后未收到明确的答错反馈，停止自动重试。')
                if not config.get('retry') and not re.search(r'重新选择|重新作答|重试|再试', text):
                    raise ValueError('页面未明确允许重试，请手动处理。')
                if config.get('retry'):
                    box.locator(config['retry']).click(timeout=4000)
            record['pending'] = False
        candidates = answer_candidates(kind, count)
        if record['index'] >= len(candidates):
            raise ValueError('该题全部选项已尝试，未获通过，请手动回答。')
        choice = candidates[record['index']]
        for index, option in enumerate(options.all()):
            if kind == 'multiple':
                option.set_checked(index in choice, timeout=4000)
            elif index in choice:
                option.check(timeout=4000)
        box.locator(config['submit']).click(timeout=4000)
        record['index'] += 1
        record['pending'] = True
        LOG.info('视频弹题尝试 %s / %s', record['index'], len(candidates))
        if config.get('correct_count'):
            frame.page.wait_for_timeout(1200)
        return True


def watch_chapter(page, quiz, *, timeout=180, poll=2):
    """完成当前视频卡片所有视频；返回 True 表示有视频，False 表示无视频卡片。"""
    tab = page.locator('li[id^="dct"][title="视频"]')
    if tab.count() == 0:
        # 加载不确定时必须停下，不能当成无视频跳过。
        try:
            tab.first.wait_for(state='attached', timeout=15000)
        except PlaywrightTimeout as exc:
            card = content_frame(page)
            if (card and card.evaluate('document.readyState') == 'complete'
                    and page.locator('li[id^="dct"]').count()
                    and not card.locator('.ans-job-video, iframe[src*="/ananas/modules/video/"]').count()):
                return False
            raise ValueError('等待视频标签加载超时，未确认本节无视频，请人工检查。') from exc
    if 'active' not in (tab.first.get_attribute('class') or '').split():
        tab.first.click()
    deadline = time.monotonic() + 45
    while not video_frames(page):
        if time.monotonic() > deadline:
            raise ValueError('视频卡片已打开，但播放器未加载。')
        page.wait_for_timeout(1000)
    frames = video_frames(page)
    index = 0
    while index < len(frames):
        frame = frames[index]
        LOG.info('监控本节第 %s/%s 个视频', index + 1, len(frames))
        last_progress = time.monotonic()
        previous = -1
        last_report = 0
        confirm_since = None
        play_since = 0
        while True:
            state = video_state(frame)
            if state['error']:
                raise ValueError(f'播放器错误代码 {state["error"]}，请检查网络后重新播放。')
            now = time.monotonic()
            if state['current'] > previous + .25:
                last_progress = now
                previous = state['current']
            if now - last_report >= 30:
                LOG.info('播放进度 %.0f / %.0f 秒', state['current'], state['duration'])
                last_report = now
            if quiz.handle(frame):
                if now - last_progress > timeout:
                    raise ValueError('弹题处理长时间未完成，请人工检查。')
                page.wait_for_timeout(2000)
                continue
            # 没有成功适配时，任何可见选择框都视为未知弹题，不能盲点播放。
            choices = frame.locator('input[type="radio"]:visible, input[type="checkbox"]:visible')
            if choices.count():
                raise ValueError('发现尚未适配的视频弹题，请人工处理；需要结构样本时可启用 --diagnostics。')
            completed = task_completed(frame)
            action = action_for(state, completed is True)
            if action == 'done':
                LOG.info('视频已播完，且任务点已完成。')
                break
            if action == 'confirm':
                confirm_since = confirm_since or now
                if now - confirm_since > 60:
                    raise ValueError('视频已结束，但一分钟内未确认视频任务点完成，请人工检查。')
            elif not state['ended'] and state['paused'] and now - play_since >= 10:
                # 初次播放时 readyState 可能为 0，仍需点击“播放视频”加载媒体。
                click_play(frame)
                play_since = now
            if now - last_progress > timeout and not state['ended']:
                raise ValueError('视频长时间没有进展，可能有未知弹题、验证或网络问题。')
            page.wait_for_timeout(int(poll * 1000))
        index += 1
        # 再枚举，避免漏掉延迟出现的第二个播放器。
        page.wait_for_timeout(500)
        frames = video_frames(page)
        card = content_frame(page)
        expected = card.locator('iframe[src*="/ananas/modules/video/"]').count()
        if len(frames) < expected:
            deadline = time.monotonic() + 30
            while len(frames) < expected:
                if time.monotonic() > deadline:
                    raise ValueError('部分视频播放器尚未加载，停止切换章节。')
                page.wait_for_timeout(500)
                frames = video_frames(page)
    return True


def advance_chapter(page):
    old = chapter_id(page)
    target = next_chapter(page)
    if target is None:
        return False
    page.locator(f'#{target} .posCatalog_name').click(timeout=10000)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if chapter_id(page) != old:
            card = content_frame(page)
            # 等待新卡片的 knowledgeid，防止 active 已更新但旧 iframe 仍在。
            if card and parse_qs(urlsplit(card.url).query).get('knowledgeid', [''])[0] == chapter_id(page):
                card.wait_for_load_state('load', timeout=30000)
                return True
        page.wait_for_timeout(500)
    raise ValueError('目录点击后没有确认进入新章节，已停止以免重复操作旧视频。')


def wait_for_course(context, page, url, seconds=900):
    LOG.info('如果出现登录页，请在新开的浏览器中手动登录；工具会自动检测学习页。')
    deadline = time.monotonic() + seconds
    last_retry = 0
    while time.monotonic() < deadline:
        for candidate in context.pages:
            if candidate.is_closed():
                continue
            if candidate.locator('#iframe').count() and candidate.locator('[id^="dct"]').count():
                LOG.info('已检测到课程学习页。')
                return candidate
        if not context.pages:
            raise ValueError('浏览器已关闭。')
        # 登录后可能停在个人首页；仅同站且登录表单已消失时重新打开目标课程。
        if time.monotonic() - last_retry > 20:
            for candidate in context.pages:
                if (candidate.locator('input[type="password"]:visible').count() == 0
                        and is_chaoxing_host(urlsplit(candidate.url).hostname)
                        and 'login' not in candidate.url.lower()
                        and 'passport' not in candidate.url.lower()):
                    candidate.goto(url, wait_until='domcontentloaded', timeout=30000)
                    break
            last_retry = time.monotonic()
        page = context.pages[-1]
        page.wait_for_timeout(2000)
    raise ValueError('等待登录超时，请重新启动后完成登录。')


def run(args):
    url, stall_timeout = load_settings(args.url)
    selectors_path = ROOT / 'quiz_selectors.json'
    selectors = read_json_object('quiz_selectors.json') if selectors_path.exists() else {}
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(ROOT / 'browser_profile'), channel='chrome', headless=False,
                viewport={'width': 1360, 'height': 900}, accept_downloads=False,
            )
        except Error as exc:
            raise ValueError('无法启动 Google Chrome。请确认已安装 Chrome，关闭其它工具实例及其浏览器后重试。'
                             f'浏览器信息：{exc}') from None
        context.set_default_timeout(6000)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            page = wait_for_course(context, page, url)
            save_position(page, url)
            if args.login:
                LOG.info('登录成功，会话已保存在本工具目录中。可以关闭浏览器，再双击 start.cmd。')
                (ROOT / 'login_ready.txt').write_text(time.strftime('%Y-%m-%d %H:%M:%S'), encoding='utf-8')
                while context.pages:
                    try:
                        context.pages[-1].wait_for_timeout(1000)
                    except Error:
                        break
                return
            if args.inspect:
                dump_diagnostics(page, '手动诊断')
                input('诊断已保存。按回车关闭工具浏览器：')
                return
            quiz = QuizHandler(selectors)
            visited = set()
            while not page.is_closed():
                current = chapter_id(page)
                if current in visited:
                    raise ValueError('重复进入同一章节，已停止。')
                save_position(page, url)
                LOG.info('当前章节 %s', current)
                try:
                    found = watch_chapter(page, quiz, timeout=stall_timeout)
                    if not found:
                        LOG.info('本节无“视频”卡片，跳过。')
                except (ValueError, PlaywrightTimeout) as exc:
                    human_pause(page, str(exc), diagnostics=args.diagnostics)
                    selectors = read_json_object('quiz_selectors.json') if selectors_path.exists() else {}
                    quiz = QuizHandler(selectors)
                    continue
                visited.add(current)
                if args.once:
                    LOG.info('单节模式已完成。')
                    break
                if not advance_chapter(page):
                    LOG.info('已经到达目录末尾。')
                    break
        except KeyboardInterrupt:
            LOG.info('已停止，最近章节保存在 progress.json。')
        except Error as exc:
            if args.diagnostics and context.pages:
                dump_diagnostics(page, f'浏览器操作失败：{type(exc).__name__}')
            raise
        finally:
            context.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='本次起始学习页；不使用历史进度')
    parser.add_argument('--login', action='store_true', help='仅打开浏览器完成登录，不播放视频')
    parser.add_argument('--inspect', action='store_true', help='登录后保存视频区域截图和结构')
    parser.add_argument('--once', action='store_true', help='只处理当前章节的视频')
    parser.add_argument('--diagnostics', action='store_true', help='异常时保存原始截图和 HTML（可能含个人信息）')
    args = parser.parse_args()
    try:
        (ROOT / 'logs').mkdir(exist_ok=True)
        handlers = [logging.StreamHandler(), logging.FileHandler(ROOT / 'logs' / 'run.log', encoding='utf-8')]
    except OSError:
        handlers = [logging.StreamHandler()]
    for handler in handlers:
        handler.setFormatter(PrivateFormatter('%(asctime)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    try:
        run(args)
    except (ValueError, Error, OSError, json.JSONDecodeError) as exc:
        LOG.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
