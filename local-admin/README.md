# PM × McKinsey 本地 Excel 上传后台

双击项目中的 `Open_Excel_Admin.command`，在浏览器打开 **http://127.0.0.1:8766/**。保留启动器的终端窗口；按 Control+C 停止。

也可以在项目目录运行：

```sh
.venv/bin/python local_admin.py --open
```

## 操作

1. 选择或拖入完整的 Tracker 工作簿（`.xlsx`，最大 20 MB）。工作簿必须含 `Tracker` 工作表和八个标准列；空白预留行、填写示例及 `Options` 不计入。
2. 点击“解析并预览”，核对有效事项数量以及相对本地的增/删/改变化。**此时不会修改本地或线上数据。** 预览保留 30 分钟，服务重启或再次上传会使旧预览失效。
3. 点击“确认导入本地”，保存本地 SQLite 历史与原始 Excel 副本，并生成可浏览的 `tracker.sqlite3`。可在“打开本地看板”查看完整看板（`http://127.0.0.1:8766/dashboard`）。
4. 如需更新线上看板，点击“检查线上连接”后再“检查并发布当前快照”。后台读取项目 `.env` 中已有的 `DATABASE_URL`，显示相对线上的增删变化；你确认后才在一个 MySQL 事务内替换线上数据。默认线上服务 `app.py` 没有任何上传写接口。

**上传或本地导入都不会自动发布线上。** 打开后台不会自动连接 MySQL，线上连接失败不影响本地导入。

## 数据与边界

- 数据目录：项目内 `local-admin-data/`（已加入 Git 与 Docker 忽略规则）。包含 `history.sqlite3`（导入历史与发布回执）、`tracker.sqlite3`（与线上同结构、可被 `app.py --db` 直接打开的看板库）和 `uploads/<SHA256>.xlsx`（原始文件副本）。整个目录备份即可。
- 解析规则与看板完全一致：复用 `app.parse_workbook`，逐行执行八个字段校验，状态只接受六个正式选项，源文件不会被修改。无有效事项或解析失败时不允许覆盖旧数据。
- 仅监听 `127.0.0.1`，不是局域网或公网服务，不要通过反向代理暴露。Host/Origin 校验与每次启动随机生成的 CSRF token 防止跨站写入；多窗口下过期的本地预览会被拒绝。
- 后台只记录你实际确认导入的版本，不保留未导入的修改，也不记录 Excel 编辑者身份。
- 线上发布通过命名锁 `pm_tracker_import` 串行化，失败整体回滚；`saves` 表会追加 `admin_workbook_publish` 审计记录。

## 回归检查（不写真实线上库）

```sh
.venv/bin/python -m unittest tests/test_local_admin.py -v
```

默认端口被占用时可用 `--port 8767`；测试通过 `--data-dir` 指向临时目录，与实际历史隔离。
