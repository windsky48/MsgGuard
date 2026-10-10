# MsgGuard · QQ 群消息违规自动审核机器人

基于 **QQ 官方机器人（botpy）** + **阿里云内容审核** 的群消息守护机器人。
自动识别群里的色情 / 暴恐涉政内容，按策略**撤回 → 警告 → 禁言 → 踢出**，全程留痕。

- 运行环境：Python 3.8+
- 机器人框架：QQ 官方机器人（`pip install qq-botpy`），非第三方协议机器人
- 审核能力：阿里云内容审核 SDK（文本 + 图片）
- 支持消息：普通文本、图片、**合并转发（msg_type=102，递归展开）**

---

## 目录

1. [工作流程](#一工作流程)
2. [项目结构](#二项目结构)
3. [前置准备](#三前置准备)
4. [安装部署](#四安装部署)
5. [配置说明](#五配置说明)
6. [运行与验证](#六运行与验证)
7. [白名单](#七白名单)
8. [管理指令](#八管理指令)
9. [数据与日志](#九数据与日志)
10. [常见问题](#十常见问题)
11. [注意事项](#十一注意事项)

---

## 一、工作流程

```
群消息（文本 / 图片 / 合并转发）
        │
        ▼
  ┌─────────────────────┐
  │ 1. 是不是机器人自己 │──是──▶ 丢弃
  └──────────┬──────────┘
             │否
             ▼
  ┌─────────────────────┐
  │ 2. 是管理指令吗     │──是──▶ 校验 WHITELIST_ADMINS → 执行 → 结束
  └──────────┬──────────┘
             │否
             ▼
  ┌─────────────────────┐
  │ 3. 命中白名单吗     │──是──▶ 完全跳过（不调用审核接口）→ 结束
  └──────────┬──────────┘
             │否
             ▼
  ┌────────────────────────────────────────┐
  │ 4. 解析内容                             │
  │   普通消息  → content / image.url       │
  │   合并转发  → 递归展开 msg_elements(102)│
  └──────────┬─────────────────────────────┘
             ▼
  ┌────────────────────────────────────────┐
  │ 5. 送阿里云内容审核（限流 + 重试）       │
  │    scenes = porn, terror                │
  └──────────┬─────────────────────────────┘
             ▼
     label != normal 且 rate ≥ 0.8 ？
        │              │
       否             是
        ▼              ▼
      结束     ┌──────────────────────────┐
               │ 6. 撤回消息（2 分钟时限） │
               └──────────┬───────────────┘
                          ▼
               ┌──────────────────────────┐
               │ 读 ViolationsNum[openid] │
               └────┬────────────────┬────┘
                <1 │                │ ≥1
                    ▼                ▼
        @警告 + 禁言 86400s      踢出群聊
           + ViolationsNum += 1
                    └────────┬───────┘
                             ▼
                写审计日志（内容/发送者/时间/
                 审核返回/违规次数/处置动作）
```

---

## 二、项目结构

```
MsgGuard/
├── bot.py            # 主入口：注册事件、事件去重、启动自检
├── config.py         # 全部环境变量配置（含内置 .env 解析器）
├── parser.py         # 消息归一化 + 合并转发递归拆解
├── moderation.py     # 阿里云审核核心：请求构造、限流重试、置信度判定
├── uploader.py       # Extension.Uploader 封装（本地图片换临时 URL）
├── qq_openapi.py     # 撤回 / 禁言 / 踢出 / 发消息（token 缓存 + 401 刷新）
├── storage.py        # ViolationsNum.json 持久化（原子写 + Windows 重试）
├── whitelist.py      # 白名单（查询 / 加白 / 移白 / 持久化）
├── handlers.py       # 处置编排 + 管理员指令
├── logger_setup.py   # 日志与审计文件
├── requirements.txt  # 依赖清单
├── .env.example      # 配置模板
├── ViolationsNum.json  # 违规次数（运行时自动生成）
└── Whitelist.json      # 白名单（运行时自动维护）
```

---

## 三、前置准备

### 1. QQ 开放平台

1. 登录 <https://q.qq.com/> → 机器人管理 → 创建机器人，拿到 **AppID / AppSecret**。
2. 在「开发 → 接口权限」中开启：
   - **群消息全量权限**（否则收不到非 @ 机器人的消息，本项目两个事件都依赖它）
   - 单聊消息（可选，用于响应管理指令）
3. 在「机器人配置」中填写回调地址。平台要求 **https / wss**，
   所以本机 `QQ_PORT` 需要用 Nginx / Caddy 反代到 443。
4. 把机器人拉进目标群，并设置为**群管理员**（否则禁言 / 踢出会因权限不足失败）。

### 2. 阿里云内容安全

1. 开通「文本审核」和「图片审核」服务。
2. 创建 AccessKey，得到 **AccessKey ID / AccessKey Secret**（RAM 子账号更安全）。

#### 地域说明（重要，容易混淆）

项目里涉及**两个不同厂商/服务**的「地域」，互不影响：

| 用途 | 由谁决定 | 本项目取值 |
|---|---|---|
| **内容审核 API** 的接入地域 | `.env` 的 `ALIBABA_CLOUD_REGION` | **`cn-beijing`（北京）** |
| `Extension.Uploader` 上传图片到 OSS | 官方扩展包内置，**固定写死在上海** | 无法改，也不需要改 |

- `ALIBABA_CLOUD_REGION` 传给 `AcsClient(ak_id, ak_secret, region)`，
  决定审核接口走哪个接入点（`green.cn-beijing.aliyuncs.com` 等）。
  内容安全在各地域都有部署，**北京可用**，本项目默认即北京。
- `Extension.Uploader` 的下载包里含 `oss-cn-shanghai` 字样，
  **那是扩展包自带的 OSS 上传配置，与审核接口地域无关，不要因为看到 shanghai 就去改它**。
- 万一你所在账号的审核服务只在某地域开通，改 `.env` 里的
  `ALIBABA_CLOUD_REGION`（可选 `cn-beijing` / `cn-shanghai` / `cn-hangzhou`）即可，无需改代码。

---

## 四、安装部署

### 1. 安装依赖

```powershell
cd e:\MsgGuard
pip install -r requirements.txt
```

依赖清单：

```
qq-botpy>=2.0.0
aliyun-python-sdk-core-v3==2.13.10
aliyun-python-sdk-green==3.6.6
oss2
aiohttp
```

### 2. 安装官方 Extension.Uploader（图片审核需要）

```powershell
curl.exe -o aliyun-python-sdk-green-extension.zip https://aligreen-shanghai-share.oss-cn-shanghai.aliyuncs.com/aliyun-python-sdk-green-extension.zip
Expand-Archive aliyun-python-sdk-green-extension.zip -DestinationPath .
```

解压后必须存在 `Extension\Uploader.py`（与 `bot.py` 同级）。
> 缺少该包不会导致崩溃，只是图片审核的「转存兜底」能力关闭，文本审核与主流程不受影响。

### 3. 配置

```powershell
copy .env.example .env
notepad .env
```

最少填这 5 项即可跑起来：

```ini
QQ_APPID=你的AppID
QQ_SECRET=你的AppSecret
QQ_PORT=8080
A_CLOUD_ACCESS_KEY_ID=LTAI...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
```

> `.env` 由 `config.py` 内置的解析器读取，不需要额外安装 `python-dotenv`。
> 真实环境变量优先级高于 `.env`（适合用系统环境变量注入密钥的场景）。

### 4. 反向代理（必须）

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    ssl_certificate     /path/cert.pem;
    ssl_certificate_key /path/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;      # 长连接，勿设太短
    }
}
```

开放平台里填 `https://bot.example.com`。

---

## 五、配置说明

所有项都能用环境变量覆盖，无需改代码。完整列表见 `.env.example`，以下是重点：

### QQ 机器人

| 变量 | 默认 | 说明 |
|---|---|---|
| `QQ_APPID` | - | 开放平台 AppID |
| `QQ_SECRET` | - | 开放平台 AppSecret |
| `QQ_PORT` | `8080` | 本地监听端口 |
| `QQ_SANDBOX` | `false` | 沙箱环境设为 `true` |
| `BOT_USER_OPENID` | 空 | 填入后自动忽略机器人自己发的消息 |

### 审核判定

| 变量 | 默认 | 说明 |
|---|---|---|
| `A_CLOUD_ACCESS_KEY_ID` | - | 同时兼容 `ALIBABA_CLOUD_ACCESS_KEY_ID` / `ACCESS_KEY_ID` |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | - | 同时兼容 `ACCESS_KEY_SECRET` |
| `ALIBABA_CLOUD_REGION` | `cn-beijing` | **内容审核接口**的接入地域，见下方「地域说明」 |
| `GREEN_SCENES` | `porn,terror` | 审核场景 |
| `VIOLATION_RATE_THRESHOLD` | `0.8` | 置信度阈值，`label != normal` 且 `rate ≥` 该值才判违规 |
| `GREEN_BIZ_TYPE` | `qq_group_guard` | 业务标识，便于控制台统计 |
| `TEXT_CHUNK_SIZE` | `1800` | 超长文本自动分片（官方单任务约 2000 字） |
| `GREEN_BODY_MODE` | `auto` | `auto` 会在 `tasks` / 扁平两种请求体间自动回退 |

### 接口稳定性（限流 / 重试）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GREEN_API_MAX_RETRY` | `1` | 失败重试次数 |
| `GREEN_API_RETRY_DELAY` | `1.0` | 重试间隔（秒） |
| `GREEN_API_MIN_INTERVAL` | `0.25` | 两次审核调用最小间隔 |
| `GREEN_API_MAX_CONCURRENCY` | `2` | 最大并发 |
| `GREEN_API_TIMEOUT` | `10.0` | 单次调用超时 |
| `IMAGE_FALLBACK_UPLOAD` | `true` | 直连审核失败时，下载图片再经 `Extension.Uploader` 上传重试 |

### 处置策略

| 变量 | 默认 | 说明 |
|---|---|---|
| `RECALL_LIMIT_SECONDS` | `120` | 官方撤回 2 分钟时限，超时只记日志 |
| `MUTE_SECONDS` | `86400` | 首次违规禁言秒数（1 天） |
| `KICK_ON_REPEAT` | `true` | 二次及以上是否踢出 |
| `REPEAT_THRESHOLD` | `1` | `ViolationsNum ≥` 该值即踢出 |
| `MODERATION_ENABLED` | `true` | 总开关，`false` 时只记录不处置 |

### 白名单 / 存储 / 日志

| 变量 | 默认 | 说明 |
|---|---|---|
| `WHITELIST_ENABLED` | `true` | 白名单总开关 |
| `WHITELIST_FILE` | `Whitelist.json` | 白名单文件 |
| `WHITELIST_ADMINS` | 空 | 管理员 openid（逗号分隔）。**留空 = 全部指令禁用** |
| `WHITELIST_RESET_VIOLATIONS` | `true` | 加白时是否顺带清零违规次数 |
| `VIOLATIONS_FILE` | `ViolationsNum.json` | 违规次数文件 |
| `LOG_DIR` | `logs` | 日志目录 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_RETENTION_DAYS` | `30` | 日志保留天数 |
| `DEBUG` | `false` | 打印完整异常堆栈 |

---

## 六、运行与验证

```powershell
python bot.py
```

启动会打印自检：

```
MsgGuard 配置自检
  模式            : 启用审核处置
  审核场景        : ['porn', 'terror']
  审核接入地域    : cn-beijing
  置信度阈值      : 0.80
  首次禁言秒数    : 86400
  二次处置        : 踢出群聊
  违规记录文件    : E:\MsgGuard\ViolationsNum.json
  日志目录        : E:\MsgGuard\logs
  历史违规人数    : 0
  白名单          : 启用（0 人，文件 E:\MsgGuard\Whitelist.json）
  白名单管理员    : 未配置（加白/移白/清零指令已禁用）
  内容审核可用    : 是
  ------------------------------
机器人已上线，开始监听群消息。
```

验证清单：

| 场景 | 预期结果 |
|---|---|
| 群友发正常文本 | 无任何动作，日志无 error |
| 机器人收到含违规词的文本 | 消息被撤回，群里出现 @警告，成员被禁言 1 天 |
| 同一成员再次违规 | 消息被撤回，成员被踢出群聊 |
| 群友发违规图片 | 同上（图片审核） |
| 转发含违规内容的合并转发 | 整条转发被撤回 |
| 白名单成员发违规文本 | **完全无动作**，且不消耗审核额度 |
| 停掉阿里云网络 / 改错密钥 | 机器人仍正常收发消息，日志出现 `内容审核接口不可用` |

Linux 下用 systemd 常驻：

```ini
# /etc/systemd/system/msgguard.service
[Unit]
Description=MsgGuard QQ Bot
After=network.target

[Service]
Type=simple
User=msgguard
WorkingDirectory=/opt/MsgGuard
ExecStart=/usr/bin/python3 /opt/MsgGuard/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now msgguard
sudo journalctl -u msgguard -f
```

---

## 七、白名单

白名单成员的消息**完全跳过审核**：不会送阿里云，也不会被撤回/禁言/踢出，
同时节省审核接口额度。适合放运营号、官方账号、被误判过的正常用户。

### 方式一：编辑文件（重启生效）

`Whitelist.json`：

```json
{
  "users": {
    "USER_OPENID_ABC": { "note": "运营号", "added_at": "2026-10-10 12:00:00" }
  }
}
```

也兼容简单写法，方便手工编辑：

```json
["USER_OPENID_ABC", "USER_OPENID_XYZ"]
```

### 方式二：管理指令（实时生效，无需重启）

先在 `.env` 里配置管理员：

```ini
WHITELIST_ADMINS=你的user_openid
```

之后在群里或私聊里发指令即可（见下一节）。

### 怎么拿 user_openid

1. 打开 `logs/msgguard.log`，任意一条 `待审核消息：group=... user=<openid>` 日志里就有；
2. 或把 `LOG_LEVEL` 设为 `DEBUG` 再发一条消息；
3. 或先用违规测试触发一次，日志与 `logs/violations_*.jsonl` 都会记录发送者 ID。

### 安全说明

- `WHITELIST_ADMINS` **默认为空，即所有管理指令禁用**，只能靠改文件维护白名单；
- 非管理员执行管理指令会被拦截并回复「无权限」，同时记录 warning 日志；
- 已知指令只有 7 个（见下表），不以 `#` 开头的其它内容一律照常送审，不会被误吞。

---

## 八、管理指令

| 指令 | 说明 |
|---|---|
| `#加白 <openid> [备注]` | 加入白名单，并按配置清零其违规次数 |
| `#移白 <openid>` | 移出白名单 |
| `#清零 <openid>` | 清零违规次数（重新给一次警告机会） |
| `#白名单` | 列出当前全部白名单成员 |
| `#查次数 <openid>` | 查询违规次数及是否为白名单成员 |
| `#帮助` / `#help` | 显示指令说明 |

两种输入方式都支持：

```
#加白 ABC123DEF 运营号          # 直接写 openid
#加白 <@!ABC123DEF>             # 群里 @ 对方后，正文会带出 <@!openid>
```

指令可在**群聊**或**私聊**中使用；私聊发任意非指令内容会返回帮助信息。

---

## 九、数据与日志

| 文件 | 说明 |
|---|---|
| `ViolationsNum.json` | `{openid: 违规次数}`，原子写 + Windows 文件占用重试，重启不清零 |
| `Whitelist.json` | 白名单，指令变更即时落盘 |
| `logs/msgguard.log` | 业务日志，按天切分，保留 `LOG_RETENTION_DAYS` 天 |
| `logs/violations_YYYY-MM-DD.jsonl` | **违规审计**，每行一条 JSON |

审计记录字段：

```json
{
  "time": "2026-10-10 19:52:36",
  "group_openid": "GRP_xxx",
  "message_id": "MSG_xxx",
  "sender_openid": "USER_xxx",
  "merged_forward": false,
  "violation_type": "text",
  "violation_source": "message",
  "violation_content": "违规内容摘要",
  "audit_result": { "code": 200, "results": [ { "scene": "porn", "label": "porn", "rate": 0.95 } ] },
  "hit_scenes": [ { "scene": "porn", "label": "porn", "rate": 0.95 } ],
  "violations_num": 1,
  "actions": ["recall", "warn", "mute"]
}
```

常用查询：

```powershell
# 最近的违规记录
Get-Content logs\violations_*.jsonl -Tail 20

# 违规次数最多的 10 个用户
(Get-Content ViolationsNum.json | ConvertFrom-Json).PSObject.Properties |
  Sort-Object Value -Descending | Select-Object -First 10 Name, Value
```

---

## 十、常见问题

**Q1：机器人收不到群消息？**
A：检查 ① 开放平台是否开了群消息全量权限；② 机器人是否已被拉进群；
③ 反代是否生效（平台需 https/wss）；④ 看日志有没有 `机器人已上线`。

**Q2：提示「内容审核接口不可用」？**
A：依次确认 AccessKey 是否正确、**内容审核接口地域是否为 `cn-beijing`**、
是否开通了文本/图片审核服务。审核不可用时**机器人不会崩**，只是不处置消息。

**Q3：撤回失败？**
A：撤回有 2 分钟时限，超时会跳过并记日志；另外确认机器人有撤回他人消息的权限。

**Q4：禁言 / 踢出没生效？**
A：机器人必须是**群管理员**。日志会打印 `禁言失败：HTTP 4xx`，按返回码排查。
注意需求里明确：接口调用失败只记录错误日志，不影响撤回。

**Q5：图片审核不准？**
A：QQ CDN 可能有防盗链。保持 `IMAGE_FALLBACK_UPLOAD=true`，
程序会自动「下载图片 → `Extension.Uploader` 上传 → 用临时 URL 重新送审」。

**Q6：明明是正常内容却被判违规？**
A：先用 `#查次数` 确认，再 `#加白` 或 `#清零`；也可以适当调高
`VIOLATION_RATE_THRESHOLD`（如 `0.9`）降低误伤。

**Q7：为什么白名单指令没反应？**
A：多半是 `WHITELIST_ADMINS` 没配或填的不是 user_openid（不是 QQ 号），
启动自检里会显示「未配置（加白/移白/清零指令已禁用）」。

**Q8：中文日志会不会乱码？**
A：不会。程序启动时已把 `stdout/stderr` 强制 reconfig 成 UTF-8，
日志文件（`logs/*.log`、`violations_*.jsonl`）也是显式 UTF-8 写入，
所以**终端直看、重定向到文件、nohup、systemd、CI 日志**看到的都是同一份正确中文。

> 唯一需要注意的是 **Windows PowerShell 5.1 自身的管道解码**（它的 `$OutputEncoding`
> 默认是 ASCII）。若你用管道转发机器人输出并出现乱码，先执行一次：
> ```powershell
> $OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
> python bot.py | Select-String "违规"
> ```
> 或直接看日志文件（推荐）：
> ```powershell
> Get-Content logs\msgguard.log -Encoding UTF8 -Tail 50
> ```
>
> 想保留自己的编码偏好，可以显式设置 `PYTHONIOENCODING`，程序检测到后会尊重你的设置：
> ```powershell
> $env:PYTHONIOENCODING = "gbk"
> ```

---

## 十一、注意事项

1. **合并转发只能撤整条**：官方仅支持撤回顶层消息，无法单独删除已被浏览的子消息。
2. **置信度量纲**：阿里云不同版本 `rate` 可能是小数或百分制，
   代码已统一归一化到 0~1 再与阈值比较，避免百分制把 `normal` 误判为违规。
3. **两个事件可能重复触发**：部分网关版本下 `on_group_message_create` 与
   `on_group_at_message_create` 会对同一条消息都触发，代码已做 openid+msg_id 去重，
   保证一条消息只审核、只处置一次。
4. **Windows 落盘**：目标 JSON 被杀软/索引占用时 `os.replace` 会报 `WinError 5`，
   已内置退避重试，不会丢违规记录。
5. **中文编码**：启动即把控制台标准流锁成 UTF-8，日志文件显式 UTF-8 写入，
   终端/重定向/CI 各处输出一致（详见 [Q8](#十常见问题)）。
6. **合规使用**：撤回、禁言、踢出会直接影响用户权益，请仅用于自己管理的群，
   事先在群公告中说明规则，并遵守 QQ 开放平台服务条款、《网络安全法》
   及《互联网信息服务管理办法》等相关规定；阿里云内容审核结果仅供参考，
   重要场景建议人工复核。