# Hz-Web

`v2026.08.29`

Hetzner Cloud 流量监控与管理面板：查看服务器流量、qBittorrent 统计、Cloudflare DNS 状态，并支持 Telegram 通知、快照重建和手动创建缺失服务器。

> 当前仓库：<https://github.com/ihanr/Hz-Web>

## 安装

要求：一台 Linux 服务器、Docker Engine 与 Docker Compose Plugin。Web 默认监听 `1227` 端口，请在防火墙或反向代理中放行该端口。

### 一键安装

以 root 执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ihanr/Hz-Web/main/scripts/install-all.sh | sudo bash
```

脚本会克隆到 `/opt/hetzner-web`，创建 `config.yaml` 和随机 Web 登录密码。终端显示的密码只显示一次，请立即保存。

已有安装更新：

```bash
curl -fsSL https://raw.githubusercontent.com/ihanr/Hz-Web/main/scripts/install-all.sh | sudo env ALLOW_UPDATE=1 bash
```

### 手动安装

```bash
sudo git clone https://github.com/ihanr/Hz-Web.git /opt/hetzner-web
cd /opt/hetzner-web
sudo cp config.example.yaml config.yaml
sudo chmod 600 config.yaml
sudo sh -c 'printf "%s\n" "{\"username\":\"admin\",\"password\":\"请替换为高强度密码\"}" > web_config.json'
sudo chmod 600 web_config.json
sudo docker compose up -d --build
```

打开 `http://服务器IP:1227`。公网使用时请放在 HTTPS 反向代理之后。

### 更新后应用配置

修改 `config.yaml` 或 `web_config.json` 后：

```bash
cd /opt/hetzner-web
sudo docker compose up -d --build
sudo docker compose ps
```

运行日志：

```bash
sudo docker compose logs -f --tail=100
```

## 配置说明

配置文件均在 `/opt/hetzner-web/`：

| 文件 | 用途 |
| --- | --- |
| `config.yaml` | Hetzner、流量阈值、qB、Telegram、Cloudflare 和重建配置 |
| `web_config.json` | Web Basic Auth 登录账号与密码 |
| `state/` | 自动维护的流量和阈值状态；不要删除 |

`config.yaml`、`web_config.json`、密钥和 `state/` 均已在 `.gitignore` 中，不能提交到仓库。

### Web 登录

`web_config.json`：

```json
{
  "username": "admin",
  "password": "替换为高强度密码"
}
```

### Hetzner 与流量阈值

```yaml
hetzner:
  api_token: "YOUR_HETZNER_API_TOKEN"

traffic:
  limit_gb: 18432       # 单台服务器出站阈值，GiB
  check_interval: 5     # 检查间隔，分钟
  exceed_action: rebuild # 超限操作：rebuild
```

Hetzner Token 需要本项目服务器、镜像、快照、SSH Key 和 Primary IP 的管理权限。`exceed_action: rebuild` 会按下面的 `rebuild` 配置执行自动重建，请先确认快照和地区回退顺序。

### qBittorrent（可选）

```yaml
qbittorrent:
  enabled: true
  counter_mode: alltime  # alltime 或 session
  rebuild_cooldown_seconds: 300
  instances:
    - name: "1"
      url: "http://1.example.com:9090"
      username: "YOUR_QB_USERNAME"
      password: "YOUR_QB_PASSWORD"
      verify_ssl: false
      timeout_seconds: 6
      login_retries: 3
      login_retry_delay: 3
```

`name` 应与 Hetzner 服务器名称一致；`url` 是 qB WebUI 地址。qB 5.2.x 登录状态码已兼容。

### Telegram（可选）

```yaml
telegram:
  enabled: true
  bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"
  notify_levels: [80, 90, 95, 100]
  daily_report_time: "23:55"
```

常用命令：`/status`、`/rebuild <服务器ID>`、`/dnsync`、`/dnscheck <服务器ID>`。

### Cloudflare DNS（可选）

```yaml
cloudflare:
  api_token: "YOUR_CLOUDFLARE_API_TOKEN"
  zone_id: "YOUR_CLOUDFLARE_ZONE_ID"
  sync_on_start: true
  update_retries: 3
  update_retry_delay: 5
  rebuild_sync_delay_seconds: 90
  sync_interval_seconds: 180
  record_map:
    "1": "1.example.com"
    "2": "2.example.com"
```

`record_map` 的键使用服务器名称；重建或手动创建成功后，程序会更新对应 A 记录。

### 重建、地区回退与手动创建

```yaml
rebuild:
  mode: snapshot
  snapshot_id_map:
    "1": "YOUR_SNAPSHOT_ID"
    "2": "YOUR_SNAPSHOT_ID"
  location_fallbacks:
    - nbg1
    - fsn1
    - hel1
  manual_create:
    enabled: true
    server_type: cx33
    server_types: [cx33, cx43, cx53]
```

- 自动超限重建始终从快照创建。
- 依次尝试 `location_fallbacks`；所有地区无容量时只通知，不会自动重复创建。
- WebUI 中“创建服务器”可选择项目快照或官方系统镜像。系统镜像创建必须选择项目内 SSH Key；镜像、规格、架构和磁盘大小会在创建前再次校验。
- `snapshot_id_map` 的键使用服务器名称，不是 Hetzner 服务器 ID。

### 定时删除/创建（可选）

```yaml
scheduler:
  enabled: false
  delete_time: "23:50"
  create_time: "08:00"
```

启用前请先在测试环境验证快照映射和 DNS 映射；定时删除是不可逆操作。

## 常用检查

```bash
cd /opt/hetzner-web
sudo docker compose ps
sudo docker compose logs --tail=100 hetzner-web
curl -I http://127.0.0.1:1227/
```

如果页面打不开，先检查容器状态、1227 端口和反向代理；如果 DNS 没更新，检查 Cloudflare Token 的 DNS 编辑权限与 `record_map`。

## 升级说明

版本采用日期格式：`vYYYY.MM.DD`。本版本为 `v2026.08.29`。

升级前建议备份敏感配置和状态：

```bash
cd /opt/hetzner-web
sudo tar czf hz-web-backup-$(date +%F).tgz config.yaml web_config.json state
```

## 安全建议

- 不要把 Token、密码、私钥、`config.yaml` 或 `web_config.json` 上传到 GitHub。
- Web 面板使用 Basic Auth；公网部署必须使用强密码和 HTTPS。
- 重建/删除前确认流量阈值、快照、Primary IP 限额和地区回退配置。

## License

[MIT](LICENSE.md)
