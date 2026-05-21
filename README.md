# 英杰大战环境数据库

本项目用于从英杰大战.NET 会员区采集环境对局数据，首版聚焦：

- 从关注列表发现主君
- 采集每日对局列表和详情页
- 标准化保存对局 ID、双方卡组、胜负、模式、版本、城血和时间线摘要
- 保存 replay/play/m3u8 地址，但默认不下载视频

## 常用命令

```powershell
python -m pip install -e .[dev]
python -m eiketsu_env init-db
python -m eiketsu_env doctor firefox
python -m eiketsu_env doctor browser
python -m eiketsu_env collect follow --date 2026-05-20
python -m eiketsu_env collect follow --date 2026-05-10 --player-id 586
python -m eiketsu_env collect follow --from 2026-05-20 --to 2026-05-20 --skip-inactive --skip-existing
python -m eiketsu_env collect follow --from 2026-04-22 --to 2026-05-04 --skip-inactive --skip-existing --concurrency-profile aggressive --auth-source auto
python -m eiketsu_env collect video-search --date 2026-05-20 --max-cards 20
python -m eiketsu_env collect video-search --from 2026-05-20 --to 2026-05-20 --version Ver.3.5.0A --max-cards 0 --skip-searched-cards --frontier-rounds auto --concurrency-profile aggressive --auth-source auto
python -m eiketsu_env export matches --format csv
python -m eiketsu_env export matches --format md
python -m eiketsu_env analyze refresh --from 2026-05-10 --to 2026-05-10 --high-ranker-rank 100
python -m eiketsu_env analyze refresh --from 2026-05-20 --to 2026-05-20 --version Ver.3.5.0A --high-ranker-rank 100
python -m eiketsu_env analyze export --report overview --format md
python -m eiketsu_env analyze export --report deck --format md
python -m eiketsu_env analyze export --report deck-visual --format html
python -m eiketsu_env analyze export --report deck-archetype-visual --format html
python -m eiketsu_env analyze export --report card --format md
python -m eiketsu_env analyze export --report deck-version --format md
python -m eiketsu_env analyze export --report card-version --format md
python -m eiketsu_env share doctor
python -m eiketsu_env share sync --contributor 你的昵称
```

默认采集会跳过 `群雄伝`、`鍛練場`、`戦祭り` 这类不适合常规环境分析的模式；如果以后要做个人游玩行为或特殊规则分析，可以额外加 `--include-solo`。

`collect video-search` 会把演武场公开视频搜索作为第二采集源：未指定 `--card-hash` 时从库内高频卡牌开始搜索，只写入新的 replay 样本或补齐缺失 replay 信息；如果同一 replay 已经由 follow 详情页采到，会保留详情页的胜负、城血、时间线和双方信息，避免轻量视频页覆盖更完整的数据。
补采历史版本时建议给 `collect follow` 加上 `--skip-inactive --skip-existing`：前者会用 follow API 的 `lastplaytime` 跳过版本开始前已不活跃的主君，后者会跳过库里已经有完整详情页字段的对局，只抓真正缺口。

## 默认数据位置

- SQLite：`data/eiketsu_env.db`
- 原始快照：`data/raw/`
- 分析导出：`data/exports/matches.csv`
- 阅读导出：`data/exports/matches.md`
- 分析报告：`data/exports/analysis_overview.*`、`analysis_deck.*`、`analysis_card.*`

## 配置覆盖

- `EIKETSU_ENV_ROOT`：覆盖项目运行根目录
- `EIKETSU_ENV_DB_URL`：覆盖 SQLite URL
- `EIKETSU_FIREFOX_PROFILE`：覆盖 Firefox profile 路径
- `EIKETSU_AUTH_SOURCE`：覆盖登录态来源，支持 `auto`、`default-browser`、`chrome`、`edge`、`firefox`、`firefox-profile`
- `EIKETSU_BROWSER_PROFILE`：覆盖 Chrome/Edge/Brave/Firefox profile 路径
- `EIKETSU_LOGIN_URL`：覆盖自动打开的登录页
- `EIKETSU_BASE_URL`：覆盖英杰大战.NET 根地址
- `EIKETSU_CARD_CATALOG_PATH`：覆盖外部卡牌主数据路径；默认读取相邻项目 `E:\eki_database_v2`，读不到时退回本仓库 `assets/card_catalog.json`

## 导出阅读性

数据库里仍然保留稳定的 `card_hash`，方便后续分析和去重；导出时会以 `E:\eki_database_v2` 的卡牌主数据为准，把卡组补成 `卡名(cost 兵种)`。如果外部库暂时没有某张卡，会显示为 `未识别卡(前8位)`，完整 hash 仍保留在 CSV 的 `player_deck` / `enemy_deck` 列里。

`--max-players` 和 `--max-matches` 只表示本轮采集上限，适合试跑，不代表某一天的全量对局数。正式统计时不要设置这两个上限，或使用 `--player-id` / `--player-name` 先按单个主君核对。

## 环境分析

`analyze refresh` 会把当前库里的对局聚合成新的分析批次；每场对局双方都计入样本，side2 胜负由 side1 反推。首版默认低门槛探索：卡组至少 3 个双方样本，卡牌至少 10 个双方样本。卡牌榜的“战局表现”表示包含该卡的 side 样本整体表现，不代表单卡直接造成局内贡献。

`analysis_deck.*` 和 `analysis_card.*` 默认按 95% Wilson Score 下界排序，并同时导出原始胜率、样本数、平均城血差、平均剩余城血、场均造成城伤、场均承受城伤、场均击破数和场均撤退数。城伤优先使用详情页“城ダメージ内訳”的合计值；缺少明细时退回到最终城血反推。

刷新分析时会额外按 `sample_scope` 和 `version_scope` 保存分层统计：全玩家、全 Ranker、高 Ranker Top100，以及每个版本内的切片。全 Ranker 来自详情页双方资料里的 `全国主君ランキング`；`--high-ranker-rank` 可以调整高 Ranker 的排名上限。`deck` / `card` 报表会横向补出全 Ranker、高 Ranker 和当前版本胜率列；`deck-version` / `card-version` 用于查看同一卡组或卡牌在不同版本里的历史变化。

`deck-visual` 会导出 HTML 图文卡组报告，默认展示 Wilson 下限最高的前 30 套完整卡组，并优先从 `E:\eki_database_v2\apps\web\public\assets\cards\card_small` 复制本地小卡图到报告旁边的 assets 目录；缺图时会自动退回文字占位，避免旧官网图片 URL 失效后出现破图。

`deck-archetype-visual` 会按“共同 Cost ≥ 5.0”把相似卡组聚为卡组分类：先按 Wilson 下限排序完整卡组，再用代表构筑吸附后续相似构筑，避免单张替换卡把样本切得过碎。

## 多人共享与汇总

`shared/share_config.json` 是多人同步的目标版本配置，包含目标版本、采集日期范围、是否包含特殊模式、报告格式和高 Ranker 口径。新版本开始后，先由维护者更新这个文件，再让朋友同步仓库；如果新版本刚开始还没有上传样本，公开榜单会显示当前版本但暂时为空，旧版本可在榜单页“目标版本”处切换查看。

版本变化跟进流程：

1. 打开官方首页确认“現在稼働中バージョン”和开始日期。
2. 先确认相邻目录 `eki_database_v2` 已经有最新官方 base 快照，然后运行准备脚本：

```powershell
python scripts\prepare_version_update.py --version Ver.3.5.0A --start-date 2026-05-20
```

脚本会更新 `shared/share_config.json`、`src/eiketsu_env/config.py` 的 `VERSION_START_DATES`，并用最新官方 base 重建 `assets/card_catalog_overlay.json`，避免 VPS fallback 卡表漏新卡。

3. 检查脚本输出的 overlay 卡数和 Git diff；新版本刚开时 `date_to` 可以先等于开始日，工具会按日本时间自动延到当天。
4. 跑测试并提交：

```powershell
pytest
git add shared/share_config.json src/eiketsu_env/config.py assets/card_catalog_overlay.json README.md
git commit -m "chore: 准备 Ver.x 新版本配置"
```

5. 部署到 VPS 后，执行脚本输出的 `set-config` 和 `refresh-leaderboard` 两条命令。
6. 通知贡献者重新同步或上传。新版本暂无上传时页面会显示空状态；旧版本榜单不要删除，用户可在公开榜单页“目标版本”处自行切换。

维护者也可以直接打开本地管理器：

```powershell
dist\EiketsuCollectorManager.exe
```

它会把检查配置、写入新版本配置和卡表 overlay、运行关键测试、复制 Git/VPS 命令、启动本地预览串成按钮流程。重新打包管理器时运行：

```powershell
scripts\build_client_exe.ps1 -Name EiketsuCollectorManager -Mode manager -NoVersionSuffix
```

推荐朋友侧使用：

```powershell
scripts\share_sync.bat
```

脚本会提示输入贡献者昵称，并保存到本地 `data/share_contributor.txt`；`data/` 已被 `.gitignore` 忽略，不会提交。也可以直接运行：

```powershell
python -m eiketsu_env share sync --contributor 你的昵称
```

一键同步会执行 Git pull、按共享配置采集 follow 数据、导出 `shared/contributions/{contributor}/{version}/...jsonl`、导入所有贡献包、刷新目标版本分析、把报告写到 `shared/reports/{version}/`，最后只提交和推送 `shared/` 下的共享配置、贡献包和报告。贡献包只包含标准化对局字段，不包含 Firefox profile、cookies、原始 HTML 或本地 SQLite。

朋友侧默认使用 `--auth-source auto`，会优先选择 Windows 默认浏览器；如果默认浏览器不可控，会自动改用已安装的 Edge / Chrome / Brave 专用登录窗口。朋友只需要在弹出的窗口登录一次，后续同步会复用本机 AppData 中的专用登录目录。可以先运行：

```powershell
python -m eiketsu_env doctor browser --auth-source auto
```

如果朋友使用了非默认 profile，可以通过 `EIKETSU_BROWSER_PROFILE` 指向对应 profile；旧的 `EIKETSU_FIREFOX_PROFILE` 和 `doctor firefox` 仍可继续使用。

低层排查命令：

```powershell
python -m eiketsu_env share export --contributor 你的昵称
python -m eiketsu_env share import
python -m eiketsu_env share aggregate
python -m eiketsu_env share doctor
```

## VPS 上传模式

普通朋友不需要 Git、本地仓库或 Python 源码。服务端部署在 VPS 后，你给朋友一个 exe、VPS 地址和一次性邀请码即可。

服务端首次部署：

先在 VPS 项目目录创建本机 `.env`，管理口令不要提交到 Git：

```bash
read -s -p "Admin token: " EIKETSU_ADMIN_TOKEN
printf '\nEIKETSU_ADMIN_TOKEN=%s\n' "$EIKETSU_ADMIN_TOKEN" > .env
```

再启动服务：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml run --rm api eiketsu-server admin set-config --target-version Ver.3.5.0A --date-from 2026-05-20 --date-to 2026-05-20
docker compose -f deploy/docker-compose.yml run --rm api eiketsu-server admin create-invite --label 朋友A
```

如果管理页已经打开但提示“还没有配置管理口令”，补好 `.env` 后执行：

```bash
docker compose -f deploy/docker-compose.yml up -d api
```

朋友侧推荐使用图形客户端；VPS 地址已内置在应用里，不需要让朋友手动输入：

```powershell
scripts\run_client_gui.bat
```

打开后会看到 `1. 绑定 -> 2. 登录 -> 3. 同步 -> 4. 查看` 的向导，按当前页面的主按钮操作即可：

1. 填写邀请码和昵称，点击“绑定邀请码”。
2. 浏览器选择建议保持“自动检测（默认浏览器优先）”；如果失败，再手动选择 Chrome、Edge 或 Brave。
3. 点击“打开登录页”，在程序打开的浏览器窗口完成会员区登录；登录后不用关闭网页，回到应用等待自动检测，成功后会直接进入第 3 步。
4. 回到应用确认目标版本和采集日期；起始日期不能早于版本开始日，可以按需改晚。
5. 点击“开始同步”，看进度条和日志，提示完成前不要关闭窗口。
6. 完成后点击“我的上传”或“排行榜”查看结果。

目标版本默认使用服务端返回的最新版本；需要补传旧版本时，可以在第 3 步的“目标版本”下拉框切换，日期范围会随版本重新计算。

打包给朋友的 Windows 单文件 exe：

```powershell
scripts\build_client_exe.ps1
```

第一次生成后发送 `dist\EiketsuCollector_0.1.9.exe` 这类带版本号的文件给朋友即可。这个版本开始会自动检查 VPS 上的新客户端；以后你只需要在 VPS 发布新版，朋友打开旧客户端时会看到下载提示。

发布新版客户端到 VPS：

```powershell
scripts\publish_client_update.ps1 -Version 0.1.9 -Notes "支持 Ver.3.5.0A 默认采集和旧版本切换"
```

管理页可查看当前发布的客户端更新包：`/admin/updates`。CLI 仍保留给排查使用：

```powershell
eiketsu-client bind --server http://你的VPS_IP:8000 --invite 邀请码 --contributor 昵称
eiketsu-client sync
eiketsu-client sync --target-version Ver.3.1.0H
```

`sync` 会从服务端读取目标版本和日期范围，默认使用最新目标版本；传 `--target-version` 可以补传旧版本。随后工具会自动检查默认浏览器登录态，必要时打开会员区登录页，然后采集、导出标准化 JSONL、上传到 VPS。上传包不包含 cookies、浏览器 profile、本地路径、raw HTML 或 SQLite。

查看页面：

- `http://你的VPS_IP:8000/me`：用户输入本机 token 后查看自己的上传批次
- `http://你的VPS_IP:8000/leaderboard`：公开匿名聚合榜
- `http://你的VPS_IP:8000/health`：健康检查

IP + HTTP 只建议小范围测试；正式给朋友长期使用前，应配置域名和 HTTPS。
