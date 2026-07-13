# imgdupe

面向大型个人图片收藏的本地以图搜图和 NovelAI 提示词标签搜索工具。

`imgdupe` 会使用感知哈希为图片建立索引，然后你可以上传、选择或粘贴一张示例图片来搜索相似图片。它也可以提取 NovelAI 提示词并建立可搜索的标签索引，同时在 SQLite 中保留原始提示词文本。

它**不会删除文件**。它只会把文件路径、元数据和哈希存入 SQLite 数据库。
如果启用缺失记录清理，删除的也只是 SQLite 里的记录，不会删除磁盘上的图片文件。

## 功能

- 递归索引一个或多个文件夹
- 使用本地 SQLite 数据库
- 使用 pHash、wHash、dHash、SHA-256 和区域网格哈希
- 本地网页界面
- 支持上传或粘贴图片进行搜索
- 支持 NovelAI 提示词标签索引和搜索
- 按数据库出现频率排序的提示词标签自动补全
- 提示词搜索结果可只显示唯一提示词
- 在可选清理缺失记录前，按 SHA-256 识别已移动文件
- 结果页支持在文件夹中显示本地图片
- 支持严格、平衡、宽松匹配模式
- 可选的深度裁剪搜索
- 可选的相似图片分组和静态审查页面
- 显示损坏或不支持文件的索引失败记录

## 安装

克隆仓库，创建虚拟环境，然后安装软件包：

```powershell
git clone git@github.com:YOUR_GITHUB_USERNAME/imageScanner.git
cd imageScanner
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .
```

安装完成后，应该可以使用 `imgdupe` 命令：

```powershell
imgdupe --help
```

如果控制台找不到 `imgdupe` 命令，可以在已激活的虚拟环境里使用模块形式：

```powershell
python -m imgdupe.cli --help
```

这两种方式都需要先安装依赖。直接在刚克隆下来的仓库里运行 `python -m imgdupe.cli`，但没有创建并安装虚拟环境，是无法工作的。

## 基本用法

递归索引一个文件夹：

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16
```

默认情况下，扫描也会尝试从图片元数据中索引 NovelAI 提示词标签。
没有提示词元数据的图片会被记录为已检查，并仍然可以通过图片哈希搜索。
如果已有数据库需要补建提示词索引，可以运行：

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --reindex-prompts
```

原始提示词会照原样保存，但用于搜索的标签会在索引时清理：
NovelAI 权重标记（例如 `1.2::tag::`）、单独的 `::`、`{}`、`[]` 以及下划线都会在搜索标签中被忽略。
例如 `sanoba_witch::` 会作为 `sanoba witch` 建立索引。

NovelAI stealth 元数据提取也可用，但默认关闭，因为它需要解码 PNG 像素，
在大图库上成本较高：

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --stealth-prompt
```

常用提示词索引扫描选项：

- `--reindex-prompts`：对未变化的文件重新读取提示词元数据。
- `--stealth-prompt`：额外尝试读取 PNG 的 NovelAI alpha 通道 stealth 元数据。
- `--no-prompt-index`：跳过提示词提取，只更新图片哈希索引。

然后启动网页界面：

```powershell
imgdupe serve --db images.sqlite
```

打开：

```text
http://127.0.0.1:8765
```

扫描命令支持增量更新。再次运行时会跳过未变化的文件：

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16
```

对于在日期排序功能加入前创建的旧数据库，同一次扫描会为未变化的文件补写
创建时间和修改时间，不会重新生成图片哈希或重新读取提示词元数据。

如果文件被移动，或者文件夹被改名，扫描新位置时会先尝试认领旧的数据库记录，
避免生成重复记录。当新路径还不在数据库中时，`imgdupe` 会在旧路径已经不存在的
记录中查找唯一的 `SHA-256 + 文件大小` 匹配。匹配成功后会保留原来的 image id、
哈希、提示词、匹配结果和分组，只更新保存的路径和文件元数据。

如果想删除移动识别后仍然找不到文件的数据库记录，可以运行：

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --delete-missing
```

`--delete-missing` 只会删除 SQLite 记录以及相关的匹配/分组记录，不会删除磁盘上的图片文件。
建议只在所有已索引的硬盘、移动盘或网络目录都已连接时使用。

也可以把多个根目录索引进同一个数据库：

```powershell
imgdupe scan "E:\Images" "F:\Downloads" --db images.sqlite --workers 16
```

## 网页界面

网页界面支持：

- 选择图片文件
- 从剪贴板粘贴图片
- 英文 / 中文语言切换
- 严格、平衡、宽松匹配模式
- 设置最低分数
- 设置最多结果数量
- 显示或隐藏完全相同的文件
- 深度裁剪搜索
- 使用英文逗号、中文逗号或换行分隔的提示词标签搜索
- 使用排除标签过滤结果，且不需要重新索引
- 提示词结果始终先按匹配标签数排序，再按创建时间、修改时间或名称排序
- 提示词标签自动补全，支持上下方向键和回车选择
- 可选择只显示唯一提示词，重复提示词只展示一张图片
- 可选删除移动识别后仍然缺失的数据库记录
- 结果卡片支持打开资源管理器并选中图片

深度模式可以在普通索引上运行：它会把查询图片临时切成许多裁剪和分块变体再搜索。如果建立了下面介绍的可选裁剪索引，效果会更好。

## 可选裁剪索引

普通索引是推荐的默认方式。它更小，并且足以处理大多数近重复图片搜索。

如果你需要更强的裁剪图片搜索能力，可以建立一个更大的裁剪索引：

```powershell
imgdupe scan "E:\Images" --db images-crop.sqlite --workers 16 --crop-index
```

这会额外存储大量裁剪区域哈希，因此数据库会明显变大。只有在裁剪图片召回率比索引体积更重要时才建议使用。

## 命令行搜索

从命令行搜索：

```powershell
imgdupe query "E:\query.jpg" --db images.sqlite --min-score 55
```

生成一个简单的 HTML 结果页：

```powershell
imgdupe query "E:\query.jpg" --db images.sqlite --html result.html --min-score 55
```

深度裁剪搜索：

```powershell
imgdupe query "E:\cropped.jpg" --db images.sqlite --tryhard --min-score 1
```

按 NovelAI 提示词标签搜索：

```powershell
imgdupe tags "1girl, blue eyes" --db images.sqlite
imgdupe tags "1girl" "blue eyes" --db images.sqlite --mode all
imgdupe tags "cat ears，smile" --db images.sqlite --mode any --limit 200
imgdupe tags "1girl" --not-tags "lowres, blurry" --db images.sqlite
imgdupe tags "1girl" --sort created-desc --db images.sqlite
```

标签搜索使用与索引相同的清理规则，因此搜索 `sanoba witch` 可以匹配包含 `sanoba_witch::` 的提示词。
排除标签直接使用已有的提示词标签索引，不需要重新扫描或重新索引数据库。

## 相似图片分组

生成视觉相似图片分组：

```powershell
imgdupe cluster --db images.sqlite --min-score 70
```

生成静态审查页面：

```powershell
imgdupe review --db images.sqlite --out review
```

打开：

```text
review\index.html
```

## 失败记录

查看索引失败的文件：

```powershell
imgdupe failures --db images.sqlite
```

网页界面里也有失败记录页面。

## 备注

- 扫描是递归的。
- 原始图片文件永远不会被修改。
- SQLite 是路径、元数据、哈希、匹配结果和分组信息的主数据库。
- 大型索引是正常现象。几十万张图片对应几 GB 数据库并不奇怪。
- 为了获得更好的扫描速度，可以调整 `--workers`；在较慢硬盘上，worker 太多反而可能变慢。
