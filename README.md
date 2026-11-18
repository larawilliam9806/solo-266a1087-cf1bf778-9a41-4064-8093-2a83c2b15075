# 冷链运输温度合规平台

单趟冷链运输的温度合规判定 Web 应用（Flask + SQLite）。核心原则：**温度读数有缺口时，未测到的时段不得算作合格，计为"未知"。**

## 判定规则

1. 温度读数按 UTC 时刻排序。相邻读数间隔 **≤ 10 分钟**时，前一读数代表该段区间 `[t_i, t_{i+1})`。
2. 相邻间隔 **> 10 分钟**时：前一读数只代表其后 10 分钟，`[t_i+10min, t_{i+1})` 整段为**未知**。
3. 首个读数之前、末次读数之后没有任何读数覆盖，属于**未知**。
4. 每个箱号只统计其在车时段 `[装车时刻, 卸车时刻)`：
   - 代表读数 **> 温度上限** → 超温段（等于上限为合格）；
   - 与未知区间相交的部分 → 未知段；
   - 缺少覆盖装车/卸车边界的读数时，边界缺测同样计入未知。
5. 输出每箱的超温总时长、未知总时长，以及对应的具体时间段。

## 修订与并发一致性

- 上传后先得到**预览**，可反复修正温度文件、装卸文件或温度上限并重新预览；未确认前不产生正式结论。
- 确认时在**单个 SQLite 事务**内整趟替换读数、装卸记录与全部箱号结论；新装卸文件中删掉的箱号，其旧结论同步删除，不留残留。
- 每个趟次维护单调递增的修订号 `revision`。确认采用 `BEGIN IMMEDIATE` + 条件更新（`WHERE revision=?`）：同一修订号的并发确认只有一个事务成功，其余得到冲突提示。
- 事务原子性保证：每次查询在显式只读事务中用同一快照读取全部表，写入则在单事务内删旧插新；因此失败或进程重启后，查询只能看到完整的旧版本或完整的新版本，不会读到跨版本的半成品。数据库文件在持久化卷上，重启不丢数据。

## 目录结构

```
app/
  __init__.py      # Flask 应用工厂、路由、修订/确认流程
  engine.py        # 时间线构建与逐箱超温/未知计算（纯函数）
  parser.py        # 两类 CSV 的解析与带行号的错误提示
  storage.py       # SQLite schema、草稿、条件更新式整趟替换
  templates/       # 页面（上传、预览、正式结论、404）
tests/             # pytest：规则、解析、端到端、并发确认、原子性
samples/           # 可直接试用的温度/装卸样例 CSV
Dockerfile, docker-compose.yml, wsgi.py, requirements.txt
```

## CSV 格式

温度 CSV（表头可识别中英文别名）：

```csv
timestamp_utc,celsius
2026-11-18T08:00:00Z,5.0
2026-11-18T08:05:00Z,6.0
```

装卸 CSV：

```csv
box,loaded_at,unloaded_at
BOX-1001,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z
```

时间为 ISO-8601；不带时区的时间按 UTC 处理。箱号不可重复，卸车时刻必须晚于装车时刻。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `coldchain-data`（容器内 `/app/data/coldchain.db`），容器重启后数据保留。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py            # 开发服务器，端口 8000
# 或：gunicorn --bind 0.0.0.0:8000 wsgi:app
```

可用环境变量 `DATABASE` 指定数据库路径。

## 测试

```bash
pip install pytest
pytest -q
```

覆盖：10 分钟边界、缺口未知段、边界缺测、半开区间不重复计时、CSV 错误提示、
预览→确认全流程、整趟替换且删除箱号无残留、同修订号并发确认仅一次成功、
连续替换期间读者只见完整版本、JSON 接口。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 趟次列表 + 新建上传表单 |
| POST | `/trips` | 上传两个 CSV 与温度上限，生成草稿并跳转预览 |
| GET | `/trips/<id>/preview` | 草稿预览（含超温/未知时长与时间段） |
| POST | `/trips/<id>/revise` | 上传修正文件/改上限，重新计算预览 |
| POST | `/trips/<id>/confirm` | 整趟替换确认；修订号过期则拒绝 |
| GET | `/trips/<id>` | 当前正式版本结论 |
| GET | `/trips/<id>/api/results` | 正式结论 JSON |
