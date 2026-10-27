# 每日新闻

个人用的新闻聚合网站：每天从 17 家主流、免费、有官方 RSS 的新闻网站抓取文章，像报纸头版一样按**栏目**和**地区**整理，英文新闻自动翻译成中文。

| | 媒体 |
|---|---|
| 英文 | BBC、卫报、天空新闻、NPR、德国之声、法国24、半岛电视台、亚洲新闻台（CNA）、CNBC、TechCrunch、Ars Technica、Engadget |
| 中文 | BBC中文（繁体自动转简体）、纽约时报中文网、德国之声中文、法广华语、中国新闻网 |

## 页面结构

参考 BBC、纽约时报、卫报等新闻网站的布局，两个维度分开：

- **栏目**（顶部导航，按内容分）：要闻 · 时政 · 财经 · 科技 · 科学与健康 · 文化 · 体育
- **地区**（导航下方，按发生地分）：全球 · 中国 · 亚太 · 欧洲 · 美洲 · 中东 · 非洲（英国在欧洲里）
- **媒体语言**（地区右侧）：全部 · 英文媒体 · 中文媒体
- 三者可以组合，例如"科技 + 美洲 + 中文媒体"；切换栏目时地区和语言保持不变
- 评论文章不单独成栏，按内容归入栏目，标题上方标"评论"

**要闻（首页）**：头条 + 3 条重要新闻 + 右侧"最新"，下面是各栏目的精选。重要程度按"有多少家媒体在报道同一件事"判断——同一事件的不同报道会归成一组，头条下方列出"另见"其他媒体的链接；其次参考 BBC、天空新闻、BBC中文首页的编辑排序。

**栏目页**：头条 + 3 条重点，下面是按时间排列的全部报道。

其他：

- **来源**：导航下方右侧，勾选想看的媒体，可一键只看英文或中文媒体
- **显示设置**（右上角）：英文新闻显示中文 / 双语 / 原文；隐藏已读
- **搜索**：右上角放大镜或按 `/`，同时搜中英文标题和摘要
- **往期**：左上角日期旁的箭头和下拉菜单，每天一页，保留 30 天
- 地址栏会记住栏目和地区（如 `index.html#s=tech&r=americas`），可以收藏；来源和显示设置保存在浏览器里
- 自动适配深色模式和手机屏幕

## 在手机上看（GitHub Pages）

网站发布在 GitHub Pages 上，手机、电脑随时随地都能打开，地址是 `https://<你的 GitHub 用户名>.github.io/news-digest/`（可以在手机浏览器里"添加到主屏幕"）。

每天英国时间早上 7:00，GitHub 会在云端自动运行 [.github/workflows/update.yml](.github/workflows/update.yml)：抓取新闻、翻译、生成网页并发布，Mac 关机也照常更新。数据库(30 天归档和已有译文)保存在 GitHub Actions 的缓存里。

- **手动更新**：GitHub 仓库页面 → Actions → 更新新闻 → Run workflow
- **开启翻译**：仓库 Settings → Secrets and variables → Actions → New repository secret，名称填 `ANTHROPIC_API_KEY`，值填你的 key
- 知道地址的人都能打开网站(页面设置了不让搜索引擎收录)；仓库代码别人能看到，但只有你的账号能修改，仓库的 Issues 和 Wiki 已关闭

## 在本机运行

```bash
./run.sh --open
```

第一次运行会自动创建 `.venv` 并安装依赖，之后抓取全部 feed（66 个，几秒钟）、翻译新的英文文章、写入 `data/news.db`，并生成 `site/index.html` 和每天的归档页 `site/2026-10-27.html`。

生成的网页可以直接双击打开，也可以开一个本地服务器（手机在同一 Wi-Fi 下访问时加 `--lan`）：

```bash
.venv/bin/python newsdigest.py serve
```

其他命令：`newsdigest.py build` 只用已有数据重新生成网页（改了模板时用）；`newsdigest.py translate` 只补翻还没翻译的文章；`update --no-translate` 这次只抓取不翻译。

## 中文翻译

英文新闻的标题和摘要由 Claude 翻译成中文（默认 Claude Haiku 5.5）。翻译要求写在 [newsdigest.py](newsdigest.py) 的 `TRANSLATE_SYSTEM` 里：忠实原意、不增删信息，人名地名用通用译名，读起来像中文媒体自己写的。

1. 在 [console.anthropic.com](https://console.anthropic.com/settings/keys) 申请一个 API key（按用量付费，和 Claude 订阅分开计费）。
2. 在终端里创建 `.env` 并打开编辑，把 key 粘贴在 `ANTHROPIC_API_KEY=` 后面，保存：

   ```bash
   cd ~/news-digest && cp .env.example .env && chmod 600 .env && open -e .env
   ```

3. 运行 `./run.sh`，之后每次都会自动翻译新抓到的英文文章。

费用：每篇只翻译一次，结果存在数据库里，重复运行不会重复收费。按每天约 470 篇英文新闻估算，Haiku 5.5 大约 $0.04/天；每次运行结束时会打印实际用量和估算花费。想要更好的译文，可以在 [sources.yaml](sources.yaml) 的 `translate.model` 换成 `claude-sonnet-5-5`（约 $0.8/天）或 `claude-opus-5-5`（约 $1.6/天）。没有设置 key 时会跳过翻译，网页照常显示原文。

## 调整新闻源和分类

全部在 [sources.yaml](sources.yaml) 里改，不用动代码：

```yaml
- id: bbc
  name: BBC
  en: BBC News
  lang: en
  feeds:
    - {url: "https://feeds.bbci.co.uk/news/rss.xml", rank: true, region: europe}
    - {url: "https://feeds.bbci.co.uk/news/business/rss.xml", section: business}
```

每个 feed 可以写：

- `section`：属于哪个栏目（`news` 时政、`business` 财经、`tech` 科技、`science` 科学与健康、`culture` 文化、`sport` 体育）
- `default`：内容比较杂的 feed，先按关键词判断栏目，判断不出再归入这个栏目
- `region`：标题和摘要里认不出地区时用的默认地区（例如英国本地新闻写 `europe`）
- `opinion: true`：评论文章
- `rank: true`：媒体首页头条 feed，用来挑选要闻

地区按 `regions` 里的国家、城市、人物关键词识别（以标题为准，标题里没有再看摘要）；没写 `section` 的文章按 `classify` 里的关键词判断栏目。英文关键词含大写字母的区分大小写（`China` 不会匹配到 china 瓷器）。

## 每天自动更新（macOS）

已经设置好：launchd 每天早上 7:00 运行 `run.sh`（配置文件 `~/Library/LaunchAgents/local.newsdigest.plist`），运行记录写在 `data/launchd.log`。

- 7:00 时电脑在睡眠，会在唤醒后补跑一次；关机状态则跳过当天。
- 唤醒后网络还没连上时，`run.sh` 会先等最多 1 分钟左右。
- 一天跑多次也没问题，重复的文章会自动去重，翻译也不会重复收费。

常用命令：

```bash
# 立刻手动触发一次
launchctl kickstart gui/$(id -u)/local.newsdigest
# 查看最近的运行记录
tail -30 ~/news-digest/data/launchd.log
# 停用自动更新
launchctl bootout gui/$(id -u)/local.newsdigest
# 重新启用
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.newsdigest.plist
```

想改时间，编辑 plist 里的 `Hour` / `Minute`，然后先停用再重新启用。项目文件夹改名或移动后，要同步修改 plist 里的路径。

## 说明

- 只抓取各网站公开 RSS 中的标题、摘要和配图，正文需要点进原网站阅读；配图直接从各媒体的服务器加载。
- 不收录付费墙网站（FT、经济学人、彭博、华尔街日报、纽约时报英文版、日经等），也不收录需要经 Google News 等第三方转接的来源。
- 栏目、地区和"同一事件"都是按关键词自动判断的，偶尔会有分错的情况。
- 标题字体用 Google Fonts 的思源宋体 / Source Serif；离线时会自动换成系统字体。
- RSS 地址偶尔会变动，页面底部和命令行输出会列出抓取失败的频道。

## 用到的开源项目

- [feedparser](https://github.com/kurtmckee/feedparser) — 解析 RSS / Atom
- [PyYAML](https://github.com/yaml/pyyaml) — 读取配置
- [OpenCC](https://github.com/yichen0831/opencc-python) — 繁体转简体
- [anthropic](https://github.com/anthropics/anthropic-sdk-python) — 调用 Claude 翻译
