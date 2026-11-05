# PM × McKinsey — EDGE AI Transformation Tracker

Publicis × EDGE 项目后续事项看板。唯一的业务分类是 **Workstream**，图表使用横向堆叠柱状图与状态数量条形图。

## 本机打开

双击本目录的 **`Start Dashboard.command`**，或执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

访问 `http://127.0.0.1:9733`。启动脚本优先使用项目虚拟环境，其次使用本机 Codex 自带的 Python；服务只监听本机回环地址，不向局域网开放。关闭运行服务的终端即停止服务。

## 数据与保存

- 首次启动从 `EDGE_AI_Transformation_Tracker.xlsx` 的 **Tracker** 工作表导入 8 个字段、10 条有效事项；空白预留行、填写示例及 Options 均不计入。
- 本地 SQL 数据库位于 **`data/tracker.sqlite3`**。数据库建立后，重新启动不会再次导入、不会覆盖浏览器已保存的编辑。后续以 SQLite 为准；修改 Excel 不会自动同步。
- 点击事项名称或行尾编辑按钮，修改原始 8 个字段。新增事项默认为 `Pending Confirmation`，事项及 Workstream 必填，未确认的负责人和日期可留空。
- **应用到草稿 → 保存更改**：应用后先更新当前页面，点击页面顶部或底部的保存按钮才写入 SQLite。刷新可恢复浏览器暂存的草稿；离开有未保存内容的页面会提醒。
- 多条修改在一个数据库事务中保存。失败时回滚并保留草稿。全局版本号防止两个窗口静默覆盖；发生冲突时，可先导出 JSON 草稿，再重新载入并手工应用差异。当前版本未提供 JSON 草稿导入。
- 原 Excel 始终只读。`saves` 表保留初始导入和每次保存的变更前后值、UTC 时间及版本号，**不记录操作人员身份**，也不据此伪造周趋势。

## 展示口径

- Workstream 聚合时，将 `GR / billing and collection` 合并到 `GR / billing & collection`，将 `Tatical plan` 展示为 `Tactical plan`；存储和编辑保留原字段值。
- 状态保持 Excel 的 6 个正式选项，不根据自由文本推断状态。工作流柱宽表示组内状态占比，右侧条形图长度表示该状态占全部事项的比例，旁边显示实际数量；不将 `In Progress` 折算为完成百分比。
- 交付率 = `Delivered / 全部有效事项`，包括待确认及暂停事项，分母在页面注明。
- 需关注 = `Blocked` 或已过目标日期且未交付、未暂停的事项，合并去重。空日期不算延期；截止日当天不算逾期。判断日期及界面时间使用北京时间。
- 信息提醒：未明确负责人或日期仅统计未交付、未暂停的事项；支持需求统计该字段非空的全部事项，不自动判为阻塞，也不擅自判定文本中的问题已解决。
- 顶部指标和图表始终为全量数据（含已应用的草稿）；筛选仅作用于下方明细。总览默认展示前 5 条，明细视图展示全部匹配事项。
- “最近保存”是数据库导入／保存时间，不是 Excel 的编辑时间或报表截止日。

## 本地数据库备份

运行中备份请使用 SQLite 原生备份，避免只复制主文件而漏掉 WAL：

```sh
sqlite3 data/tracker.sqlite3 '.backup data/tracker-backup.sqlite3'
```

也可先停止服务，再复制数据库文件。不要删除数据库来“重新导入”，否则会丢失本地保存内容；请先备份。数据库与备份都包含内部业务数据，不要提交到公开仓库。

## 测试

后端测试仅使用临时 SQLite 数据库，不修改工作数据库或源 Excel：

```sh
.venv/bin/python -m unittest discover -s tests -v
```

如已安装 Playwright 和 Chrome，运行浏览器端到端测试：

```sh
PYTHON=.venv/bin/python node tests/test_ui.cjs
```

在本机 Codex 依赖环境中可运行：

```sh
NODE_PATH="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules" \
  node tests/test_ui.cjs
```

浏览器测试自动启动独立临时数据库，覆盖编辑、新增、草稿恢复、保存失败、版本冲突、HTML 转义及移动端；截图输出到 `qa/`。

## 当前边界

这是**单机版**，无远程数据库、登录权限、人员审计、Excel 自动同步或线上部署。需要团队共享时，再接入认证和正式 SQL 服务；不要直接把本机 HTTP 服务暴露到公网。
