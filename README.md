# B2B 分销自动化

这是一个覆盖询价接入、结构化抽取、运营补全、定价和汇率、脱敏报价、客户匹配、真实渠道分发、订单履约以及防篡改审计的全栈应用。后端使用 FastAPI、SQLAlchemy、Alembic、Celery 和 Redis；前端使用 React。

## Docker Compose 启动

复制环境变量模板并补齐 MySQL 账号。所有密码和密钥只保存在 `.env`，该文件已被 Git 忽略。

```bash
cp .env.example .env
docker compose up --build
```

服务地址：

- 运营工作台：http://localhost:5173
- 后端接口：http://localhost:8000
- OpenAPI：http://localhost:8000/docs

Compose 会启动 API、Celery worker、Celery beat、Redis 和前端。API 容器先执行 Alembic 迁移，健康检查通过后 worker、beat 和前端才会启动。Redis 使用 AOF 和命名卷持久化；上传文件保存在 `uploads` 命名卷。

`BUILD_DATABASE_URL_FROM_MYSQL=true` 时，容器入口会从 `MYSQL_*` 变量安全构建并 URL 编码数据库地址，避免密码经过 Compose 字符串插值。将它设为 `false` 后，容器会直接使用 `DATABASE_URL`。

## 核心流程

1. 运营人员录入上游消息或导入 Excel，系统通过模型和规则抽取品牌、SKU、采购价、MOQ、行业与区域。
2. 缺失信息会生成运营待办；完整询价可按最新同步汇率、利润率和运费生成版本化报价。
3. 下游报价会过滤采购价、供应商和联系方式，并可导出 PDF 或 XLSX。
4. 客户按品牌模糊度、SKU 规则、行业和区域计算匹配分数，经人工确认后进入分发队列。
5. Celery worker 通过 SMTP、企业微信或飞书发送；临时失败按指数退避重试，beat 定期补扫队列并同步汇率。
6. 报价可转为订单，继续管理订单明细、库存预留与变动、付款和物流事件。
7. 操作写入哈希链审计日志。数据库触发器禁止对 `audit_logs` 执行 UPDATE 或 DELETE。

## 登录与生产初始化

开发环境可以保留：

```dotenv
APP_ENV=development
AUTH_DISABLED=true
```

此时请求以本地管理员身份运行，适合单机开发。Compose 不再强制关闭鉴权，实际值来自 `.env`。

生产环境至少需要配置：

```dotenv
APP_ENV=production
AUTH_DISABLED=false
JWT_SECRET=至少32位的随机字符串
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=至少10位的初始密码
BOOTSTRAP_ADMIN_DISPLAY_NAME=系统管理员
```

首次启动会在空 `users` 表中创建管理员。确认管理员可以登录后，从部署环境删除 `BOOTSTRAP_ADMIN_PASSWORD` 并重启服务；已有账号不会被重复创建。`API_TOKENS` 是可选的服务令牌映射，格式为 JSON，例如 `{"report-reader":"viewer"}`，不要把浏览器用户密码写入其中。

## 渠道配置与客户目标格式

客户的 `channel` 只能是 `email`、`wecom` 或 `feishu`。`channel_target` 的约定如下：

| 渠道 | `channel_target` 格式 | 所需环境变量 |
| --- | --- | --- |
| 邮件 | 一个邮箱，或以逗号/分号分隔的多个邮箱；为空时使用客户的 `email` | `SMTP_*` |
| 企业微信应用消息 | 企业成员 user ID；多个成员可使用企业微信支持的 `user1\|user2` 格式 | `WECOM_CORP_ID`、`WECOM_SECRET`、`WECOM_AGENT_ID` |
| 企业微信群机器人 | 官方 `https://qyapi.weixin.qq.com/...` webhook；也可填 `default`、`env` 或 `webhook` 读取环境变量 | `WECOM_WEBHOOK_URL` |
| 飞书应用消息 | `open_id:ou_xxx`、`user_id:xxx`、`union_id:xxx`、`email:a@b.com` 或 `chat_id:oc_xxx`；无前缀时使用 `FEISHU_RECEIVE_ID_TYPE` | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| 飞书群机器人 | 官方 `https://open.feishu.cn/...` 或 `https://open.larksuite.com/...` webhook；也可填 `default`、`env` 或 `webhook` | `FEISHU_WEBHOOK_URL` |

系统只接受企业微信和飞书官方 HTTPS webhook 域名。SMTP 成功、应用 API 成功表示服务商已接收，不等同于最终送达。需要最终送达状态时，渠道侧应调用：

```text
POST /api/delivery-receipts/{channel}
X-Delivery-Signature: sha256=<HMAC-SHA256(raw request body, DELIVERY_WEBHOOK_SECRET)>
```

请求体包含 `provider_message_id`、可选的 `provider_event_id`、`status` 和 `payload`。`status` 支持 `accepted`、`sent`、`delivered`、`read`、`failed`、`bounced`。

## 企业微信内部群询价监听

`WECOM_WEBHOOK_URL` 只负责向群里发送消息，不能接收群消息。内部群入站使用企业微信智能机器人的 API 长连接模式：在企业微信管理后台创建智能机器人，选择 API 模式和长连接，取得 Bot ID 与 Secret，然后在 `.env` 配置：

```dotenv
WECOM_AIBOT_ID=
WECOM_AIBOT_SECRET=
WECOM_AIBOT_WS_URL=wss://openws.work.weixin.qq.com
WECOM_AIBOT_ALLOWED_CHAT_IDS=
WECOM_AIBOT_ACCEPT_SINGLE_CHAT=false
WECOM_AIBOT_AUTO_REPLY=true
```

`WECOM_AIBOT_ALLOWED_CHAT_IDS` 为空时会接收机器人可见的全部内部群。生产环境建议填写允许入站的群 `chatid`，多个值用逗号分隔。启动 API、worker 和监听器：

```bash
docker compose --profile wecom-aibot up -d --build
docker compose logs -f wecom-aibot-listener worker
```

把智能机器人加入内部群后，需要在消息中 `@机器人`：

```text
@询价机器人
品牌：Nike
SKU：DD123
采购价：100 CNY
MOQ：20
行业：运动零售
区域：上海
交货地：上海
```

监听器收到文本、图文混排中的文本或语音转写后，会立即提交 Celery 任务并回复接收结果。worker 使用企业微信 `msgid` 创建幂等询价；企业微信重复投递或 worker 重试不会重复创建。结果出现在前端“询价中心”，来源渠道为 `wecom_aibot`。

智能机器人不能加入包含微信客户的外部客户群、互联群或上下游群。此类群必须使用企业微信付费的“会话内容存档”能力和官方 Finance SDK，并完成存档成员授权、客户知情同意及企业可信 IP 配置。

## 企业微信外部客户群询价监听

外部客户群入站通过 Finance SDK 定时拉取会话内容存档。启用前需要在企业微信管理后台完成以下工作：

1. 开通会话内容存档，将参与外部客户群的企业成员加入存档范围，并完成客户侧的会话存档告知与同意。
2. 创建 RSA 2048 密钥对，把公钥及版本配置到企业微信，私钥只放在部署环境变量或只读密钥挂载目录。密钥轮换后，近五天的消息仍可能使用旧版本公钥。
3. 把 worker 所在服务器的公网出口 IP 加入企业微信调用 IP 白名单。
4. 下载与 Docker worker 架构一致的官方 Linux Finance SDK，把 `libWeWorkFinanceSdk_C.so` 放入 `backend/vendor/wecom-finance-sdk/`。该专有二进制不会提交到仓库。

最低配置示例：

```dotenv
WECOM_ARCHIVE_ENABLED=true
WECOM_ARCHIVE_CORP_ID=wwxxxxxxxx
WECOM_ARCHIVE_SECRET=会话内容存档专用Secret
WECOM_ARCHIVE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
WECOM_ARCHIVE_PRIVATE_KEY_VERSION=1
WECOM_ARCHIVE_ROOM_IDS=wrxxxxxxxx
WECOM_ARCHIVE_EXTERNAL_ONLY=true
```

密钥轮换时可按版本配置多个私钥：

```dotenv
WECOM_ARCHIVE_PRIVATE_KEYS_JSON={"1":"file:/opt/wecom-finance-sdk/wecom-v1.pem","2":"file:/opt/wecom-finance-sdk/wecom-v2.pem"}
```

默认只处理外部联系人发出的群文本，并要求消息包含“询价/报价”，或至少包含两个结构字段标签，例如“品牌 + SKU”。也可以设置 `WECOM_ARCHIVE_TRIGGER_PREFIX=#询价` 强制使用前缀；`WECOM_ARCHIVE_ROOM_IDS` 用于限制允许进入询价中心的群。

当前内部群测试时，将测试成员加入存档范围，并临时设置：

```dotenv
WECOM_ARCHIVE_EXTERNAL_ONLY=false
WECOM_ARCHIVE_ROOM_IDS=内部测试群roomid
```

重建并启动服务后，可以在 worker 容器中手动执行一次轮询：

```bash
docker compose up -d --build
docker compose exec worker python -c 'from app.tasks import poll_wecom_conversation_archive; print(poll_wecom_conversation_archive.run())'
docker compose logs --tail=200 worker
```

游标保存在 `wecom_archive_cursors`，数据库租约避免同一来源重叠轮询；消息继续使用企业微信 `msgid` 幂等创建询价。单条坏消息默认重试三次，随后写入 `wecom_archive_failures` 和防篡改审计日志，再继续拉取后续消息。死信记录保存重放所需的加密 envelope；日志和审计只记录 `seq`、`msgid`、`publickey_ver` 和错误类型，不记录完整密文。

修复私钥版本或 SDK 问题后，可使用 `wecom_archive_failures.id` 手动重放某条死信：

```bash
docker compose exec worker python -c 'from app.tasks import replay_wecom_archive_dead_letter; print(replay_wecom_archive_dead_letter.run(123))'
```

把示例中的 `123` 替换成失败记录 ID。重放复用客户群、外部联系人和询价候选过滤，并继续按 `msgid` 幂等创建询价；成功后状态更新为 `replayed`。`0004` 之前已经存在且没有保存加密 envelope 的旧死信会明确报告无法重放。未开通存档能力、缺少存档专用 Secret/RSA 私钥、SDK 架构不匹配或出口 IP 未加入白名单时，worker 会给出配置或 SDK 错误，不会把轮询标记为成功。

## 数据库迁移

生产部署必须使用 Alembic，且应设置 `AUTO_CREATE_SCHEMA=false`。Compose 已完成此设置。

```bash
cd backend
alembic current
alembic upgrade head
```

迁移历史分为：

- `0001`：原有询价、报价、客户、运营任务、分发和审计表。
- `0002`：渠道回执、汇率、用户、复杂匹配字段、订单、库存、付款、物流以及审计哈希链和数据库触发器。
- `0003_wecom_archive`：企业微信会话内容存档游标、轮询租约和失败/死信记录。
- `0004_wecom_archive_replay`：为新死信保存按版本加密的 envelope，以支持修复后重放。

容器入口兼容没有 `alembic_version` 的旧安装：检测到完整旧表后先 `stamp 0001`，再升级到最新版本。`0002` 会给现有审计记录计算 `previous_hash` 和 `record_hash`。SQLite 与 MySQL 都会创建阻止审计日志更新和删除的触发器。

备份数据库后再执行生产迁移。迁移期间不要运行旧版本 API 或 worker，避免旧代码写入处于变更中的表。

## 环境变量

### 应用、数据库与认证

| 变量 | 默认/示例 | 用途 |
| --- | --- | --- |
| `APP_ENV` | `development` | 运行环境；生产使用 `production` |
| `DATABASE_URL` | `sqlite:///./b2b_agent.db` | 本地或 `BUILD_DATABASE_URL_FROM_MYSQL=false` 时的 SQLAlchemy URL |
| `BUILD_DATABASE_URL_FROM_MYSQL` | `true` | Compose 容器从 `MYSQL_*` 安全构建连接 URL |
| `RUN_MIGRATIONS` | `true` | 容器启动前执行 Alembic；Compose 只在 API 服务启用 |
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | — | MySQL 或阿里云 RDS 连接参数 |
| `CORS_ORIGINS` | `http://localhost:5173` | 逗号分隔的前端来源 |
| `UPLOAD_DIR` | `./uploads` | 导入与导出文件目录 |
| `AUTO_CREATE_SCHEMA` | 开发为 `true` | 是否调用 `create_all`；生产应关闭并使用 Alembic |
| `AUTH_DISABLED` | 开发为 `true` | 是否绕过鉴权 |
| `API_TOKENS` | JSON 对象 | 服务令牌到 `admin/operator/viewer` 的映射 |
| `JWT_SECRET` | 开发占位值 | JWT 签名密钥；生产至少 32 位 |
| `JWT_ALGORITHM` | `HS256` | JWT 算法 |
| `JWT_EXPIRE_MINUTES` | `480` | 登录令牌有效期 |
| `BOOTSTRAP_ADMIN_*` | — | 首次启动管理员的用户名、密码和显示名 |

### 模型、汇率与任务队列

| 变量 | 默认/示例 | 用途 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | — | 通义千问 API 密钥；为空时使用规则抽取 |
| `DASHSCOPE_BASE_URL` | DashScope compatible-mode | 模型 API 基础地址 |
| `MODEL_NAME` | `qwen-plus` | 抽取模型 |
| `MODEL_TIMEOUT_SECONDS` | `30` | 模型请求超时 |
| `DEFAULT_FX_RATE` | `7.20` | 没有同步汇率时的 USD/CNY 兜底值 |
| `FX_API_URL` | `https://api.frankfurter.dev/v1/latest` | 兼容 `base/date/rates` 响应的汇率 API |
| `FX_API_KEY/FX_API_KEY_HEADER` | — / `apikey` | 可选的汇率服务鉴权 |
| `FX_TIMEOUT_SECONDS` | `15` | 汇率请求超时 |
| `FX_BASE_CURRENCY` | `USD` | 手动获取汇率时的默认基准币种 |
| `FX_SYMBOLS` | `CNY,USD,EUR,GBP,JPY,HKD` | 目标币种列表 |
| `FX_SYNC_PAIRS` | `USD/CNY,EUR/CNY,GBP/CNY` | 定时持久化的币种对 |
| `FX_SYNC_INTERVAL_SECONDS` | `3600` | 汇率同步周期，最小 60 秒 |
| `REDIS_URL` | `redis://redis:6379/0` | Redis 通用地址 |
| `CELERY_BROKER_URL` | Redis DB 0 | Celery broker |
| `CELERY_RESULT_BACKEND` | Redis DB 1 | Celery 结果存储 |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | 测试时同步执行 Celery 任务 |
| `ENQUEUE_DISTRIBUTIONS` | `true` | 确认分发后立即提交 Celery 任务；测试可关闭 |
| `DISTRIBUTION_SCAN_INTERVAL_SECONDS` | `30` | 待发送记录补扫周期 |
| `DISTRIBUTION_SCAN_LIMIT` | `200` | 单次补扫上限 |
| `DISTRIBUTION_MAX_ATTEMPTS` | `5` | 默认最大发送次数 |
| `DISTRIBUTION_RETRY_BASE_SECONDS` | `30` | 指数退避初始时间 |
| `DISTRIBUTION_RETRY_MAX_SECONDS` | `3600` | 单次退避上限 |
| `DISTRIBUTION_STUCK_SECONDS` | `900` | `sending` 状态超时后的接管时间 |

### 外发渠道

| 变量 | 用途 |
| --- | --- |
| `CHANNEL_HTTP_TIMEOUT_SECONDS` | 企业微信和飞书共享 HTTP 超时 |
| `DELIVERY_WEBHOOK_SECRET` | 送达回执 HMAC 密钥 |
| `SMTP_HOST/PORT/USERNAME/PASSWORD` | SMTP 服务器与账号 |
| `SMTP_FROM_EMAIL/SMTP_FROM_NAME` | 发件地址与显示名 |
| `SMTP_USE_TLS/SMTP_USE_SSL` | STARTTLS 或隐式 SSL，只能启用一个 |
| `SMTP_TIMEOUT_SECONDS` | SMTP 连接超时 |
| `WECOM_CORP_ID/WECOM_SECRET/WECOM_AGENT_ID` | 企业微信自建应用凭据 |
| `WECOM_CORP_SECRET` | `WECOM_SECRET` 的兼容别名，配置时优先使用 |
| `WECOM_WEBHOOK_URL` | 企业微信群机器人默认 webhook |
| `WECOM_API_BASE_URL/WECOM_TIMEOUT_SECONDS` | 企业微信 API 地址与超时 |
| `WECOM_AIBOT_ID/WECOM_AIBOT_SECRET` | 内部群智能机器人长连接凭据 |
| `WECOM_AIBOT_WS_URL` | 智能机器人长连接地址，默认使用企业微信官方地址 |
| `WECOM_AIBOT_ALLOWED_CHAT_IDS` | 允许创建询价的内部群 `chatid` 白名单，逗号分隔 |
| `WECOM_AIBOT_ACCEPT_SINGLE_CHAT` | 是否接受机器人单聊消息，默认关闭 |
| `WECOM_AIBOT_AUTO_REPLY` | 入队后是否在原会话回复接收结果 |
| `WECOM_ARCHIVE_ENABLED` | 是否启用外部客户群会话内容存档轮询，默认关闭 |
| `WECOM_ARCHIVE_CORP_ID/WECOM_ARCHIVE_SECRET` | 企业 ID 与会话内容存档专用 Secret |
| `WECOM_ARCHIVE_PRIVATE_KEY*` | RSA 私钥、私钥文件或按 `publickey_ver` 配置的版本映射 |
| `WECOM_ARCHIVE_SDK_HOST_DIR/WECOM_ARCHIVE_SDK_LIBRARY_PATH` | 官方 Linux Finance SDK 的宿主目录与容器动态库路径 |
| `WECOM_ARCHIVE_ROOM_IDS` | 允许进入询价中心的群 `roomid` 白名单，逗号分隔 |
| `WECOM_ARCHIVE_EXTERNAL_ONLY` | 是否只接收外部联系人；内部群测试时设为 `false` |
| `WECOM_ARCHIVE_FILTER_CANDIDATES/WECOM_ARCHIVE_TRIGGER_PREFIX` | 询价候选过滤与可选强制前缀 |
| `WECOM_ARCHIVE_POLL_INTERVAL_SECONDS/WECOM_ARCHIVE_BATCH_SIZE` | 轮询周期和每批数量；单批最多 1000 |
| `WECOM_ARCHIVE_POISON_MAX_ATTEMPTS` | 单条消息进入死信前的尝试次数，默认 3 |
| `FEISHU_APP_ID/FEISHU_APP_SECRET` | 飞书自建应用凭据 |
| `FEISHU_WEBHOOK_URL` | 飞书群机器人默认 webhook |
| `FEISHU_API_BASE_URL/FEISHU_TIMEOUT_SECONDS` | 飞书 API 地址与超时 |
| `FEISHU_RECEIVE_ID_TYPE` | 无前缀飞书目标的 ID 类型，默认 `open_id` |

完整可复制配置位于 `.env.example`。

## 本地开发

本地开发默认使用 SQLite。若 `.env` 中启用了 `BUILD_DATABASE_URL_FROM_MYSQL`，该变量只被 Docker 入口脚本处理，不影响直接运行 Python。

```bash
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/pytest
.venv/bin/uvicorn app.main:app --reload --env-file ../.env
```

```bash
cd frontend
npm install
npm run dev
```
