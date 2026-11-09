# 维护者发布说明

## 使用整理后的源码目录

整理后的干净源码目录可以直接作为 GitHub 仓库根目录；根目录应能直接看到 `README.md`、`LICENSE` 和 `video_tool.py`。使用 Git 提交或网页上传文件时，请保留 `.github/`、`.gitignore` 和 `.gitattributes`，避免漏掉隐藏文件；不要连同上一级发布或本机验证目录一起上传。

在此目录安装依赖、填写配置或运行程序后，会产生新的本地文件。再次提交时应以 `release-files.txt` 为准，重新检查暂存内容，不能把运行过的整个文件夹直接打包公开。

## 公开文件清单

`release-files.txt` 是首版公开文件的完整清单。新增公开文件时同步更新清单；本地配置、浏览器资料、进度、日志、诊断、真实截图和开发过程资料不在清单内。

不要上传整个工作目录。`.gitignore` 仅防止新文件被默认加入，不能清除已提交的历史内容，也不能保护手动压缩或网页拖入的文件。

## 首次提交前

在项目目录运行测试和帮助命令：

```powershell
python -m unittest -v test_video_tool test_release test_browser
python video_tool.py --help
```

确认所有清单文件均已检查，没有个人课程 URL、姓名、学号、邮箱、Cookies、访问参数或本机路径。图片与 HTML 需要人工检查，不能仅依赖关键词扫描。

首次公开仓库建议只加入清单内文件。以下命令由维护者决定提交时运行：

```powershell
git init -b main
$publicFiles = Get-Content -Encoding UTF8 release-files.txt
git add -- $publicFiles
git diff --cached --check
git diff --cached --name-only
git diff --cached
git status --short
```

若已经有 Git 仓库，跳过 `git init`；先检查已有索引、分支和历史，不要假定清单会自动排除以前暂存的文件。

提交前检查 `git var GIT_AUTHOR_IDENT`。使用自己的公开 GitHub 身份，邮箱建议从 GitHub 设置中复制 noreply 地址；可通过仓库级 `git config user.name` 和 `git config user.email` 配置，不需要使用真实姓名。确认暂存内容和身份后，再自行创建首次提交、GitHub 仓库及推送。

若敏感数据曾经公开，删除当前文件或补充忽略规则不能撤回泄露。应检查 Git 历史、旧发行包及附件，并视暴露情况使相关会话或凭据失效。

## 从版本控制内容生成源码包

提交完成后，先确认工作区中所有准备发布的修改均已进入目标提交，再执行：

```powershell
New-Item -ItemType Directory -Force dist | Out-Null
git archive --format=zip --prefix=chaoxing_video_tool/ --output=dist/chaoxing_video_tool-source.zip HEAD
```

这个 ZIP 只包含目标提交中的文件，不包含未跟踪的本地配置或运行资料。打包前仍必须确认该提交本身没有敏感内容。

尚未提交时，可先按清单复制出一个干净副本用于安装验证。在该副本内安装依赖、查看帮助并运行测试，确保没有依赖本机原始目录的隐式文件。未经提交的预览包应明确标记为预览，不冒充已发布版本。

## GitHub 设置

- 许可证为 MIT，使用项目贡献者集体署名。
- Actions 使用 Windows、Python 3.10、系统 Chrome；测试不需要账号或仓库 Secrets。
- Issue 模板提醒用户不要提交个人链接、原始日志及浏览器目录。
- 首版暂不提供 EXE，也不承诺其他操作系统兼容。
