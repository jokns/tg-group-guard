# Cloudflare Turnstile + Telegram Mini App 入群验证部署指南

> 本功能为可选模式。默认保持原有数学题模式，升级后不会自动切换。首次启用请先在测试群验证。
>
> **安全提示**：此验证只能提高自动化入群成本，不保证阻止真人代验、验证码打码平台或真实设备自动化。

## 一、功能说明

管理后台 → **入群验证方式（独立保存）**：

- 数学题（默认）：简单（两位数加减）、中等（乘减混合）、困难（除法及运算优先级），每次最多答错两次，第二次失败后同一群组/用户冷却 10 分钟。
- Cloudflare Turnstile：Telegram 私信中的 **打开验证网页** 按钮启动 Mini App；服务器验证 Telegram `initData` 的 HMAC 签名和时效，再向 Cloudflare Siteverify 验证 Turnstile token，最后按入群模式批准申请或解除禁言。
- 申请入群与直接入群（先禁言）均支持；切换验证模式不会修改 OCR、观察期、关键词或日志保存设置。
- 网页域名可以后台修改；更换后须同步配置 DNS、Turnstile 允许的主机名和 BotFather（如已配置）。切换后旧域名如果停止服务，旧链接可能失效；建议等待所有旧会话超时后再关闭旧域名。

## 二、获取 Cloudflare Site Key 和 Secret Key

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)。
2. 进入 **Turnstile**（不同版本控制台可能在 **Turnstile** 或 **Security → Turnstile**）。
3. 点击 **Add widget / 添加小组件**，填写名称，例如 `TG Group Guard`。
4. **Hostname / 主机名** 添加你准备使用的二级域名，例如 `verify.example.com`。不要填写 `https://` 或路径。
5. 推荐 **Managed** 模式。创建后分别复制 **Site Key**（公开，供前端渲染）和 **Secret Key**（机密，仅供后端验证）。
6. 进入机器人后台，填写 HTTPS 验证地址、Site Key、Secret Key；验证方式选择 **Cloudflare Turnstile**，点击 **单独保存验证配置**。
7. Secret Key 留空表示保留已保存密钥。**不要将 Secret Key 放在 Nginx 配置、前端 JS、GitHub 或聊天截图中。**

官方参考：
- https://developers.cloudflare.com/turnstile/get-started/
- https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
- https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

## 三、DNS、SSL 与服务器配置

示例域名：`verify.example.com`，机器人 Docker 映射在本机 `127.0.0.1:18081:8080`。

1. Cloudflare DNS 新增 `verify` 的 A/AAAA 记录指向服务器（或 CNAME 指向现有入口）；可启用橙云代理。
2. Cloudflare SSL/TLS 推荐 **Full (strict)**；源站在宝塔/Nginx 配置有效 HTTPS 证书。
3. 为 `verify.example.com` 创建独立 Nginx 站点，**只开放** `/verify` 与 `/verify/api/complete`；禁止把整个机器人后台暴露到该域名。
4. 保证 Telegram 客户端可访问该 HTTPS 域名，且服务器能访问 `https://challenges.cloudflare.com/turnstile/v0/siteverify` 与 Telegram Bot API。
5. 如果通过 Cloudflare WAF / Bot Fight Mode / Challenge 页面保护该域名，请对验证页面和完成接口配置适当的 **跳过额外 JS Challenge** 规则，避免 Telegram 内置浏览器被二次拦截。**不要关闭 Turnstile 本身**。
6. 对 `/verify*` 配置 **Cache Bypass**，不要缓存一次性会话页面或 API 返回。

宝塔 Nginx 站点示例（请根据实际证书和监听端口调整）：

```nginx
server {
    listen 443 ssl http2;
    server_name verify.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    location = /verify {
        proxy_pass http://127.0.0.1:18081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 20s;
        add_header Cache-Control "no-store" always;
    }

    location = /verify/api/complete {
        limit_except POST { deny all; }
        client_max_body_size 16k;
        proxy_pass http://127.0.0.1:18081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 30s;
        add_header Cache-Control "no-store" always;
    }

    location / { return 404; }
}
```

**不要直接复制覆盖宝塔已有站点配置**：请将上述 location 规则合并到对应 HTTPS 站点，证书路径、listen、限速模块等按实际环境配置。源站如果不在同机，需将 proxy_pass 指向实际受保护的内网服务。

## 四、Telegram BotFather 设置

- 打开 [@BotFather](https://t.me/BotFather)，选择当前机器人。
- 如客户端或机器人配置要求 Web App 域名绑定，使用 BotFather 的域名设置功能（例如 `/setdomain`，具体菜单以 BotFather 当前显示为准）配置 `verify.example.com`。
- 本项目使用 **私信消息中的 Web App 按钮**，不需要额外发布到机器人菜单；Telegram Mini App 将提供 `initData`，后端必须校验其签名。
- 必须通过机器人私信按钮打开，直接把 URL 粘贴到普通浏览器通常无法获得有效的 `initData`，验证会拒绝。
- 机器人需要有 **批准入群申请、限制成员、封禁成员、删除消息** 权限。

## 五、部署与验证

```bash
cd /www/wwwroot/tgxbot/tg-group-guard-main
# 合并 PR 并更新代码后：
docker compose up -d --build
docker compose logs -f --tail=100
```

建议按顺序测试：

1. 后台先保持 **数学题模式**，测试简单/中等/困难，连续答错一次可重试，第二次失败并冷却 10 分钟。
2. 记录当前 OCR、观察期、白名单和日志期限；只点击 **单独保存验证配置**，刷新并确认这些设置完全没有变化。
3. 验证新域名 HTTPS 正常，Nginx `/verify` 可访问，`/settings` 在验证域名下返回 404。
4. 切换 Cloudflare 模式，测试 **申请入群**：私信 Web App → Turnstile → 自动批准。
5. 测试 **直接入群**：群内临时禁言 → 私信 Web App → Turnstile → 自动解除禁言。
6. 测试把验证链接转发给另一 Telegram 账号：因 `initData` 用户 ID 不匹配应失败。
7. 测试过期会话、重复提交、无效签名、Turnstile 错误 token，均不得放行。
8. 测试在后台切换模式后，原先已经发出的会话仍按创建时的模式验证。
9. 检查 Docker 日志中有成功/失败记录，但**不应记录 Bot Token、Turnstile Secret Key、完整 initData 或验证码 token**。

## 六、常见问题

**验证页面 404**：检查 Nginx 是否只放行了 `/verify`、`/verify/api/complete`，并确认 Host 为后台配置的域名。

**Turnstile 显示域名不匹配**：到 Cloudflare Turnstile 小组件中检查允许的 Hostname，注意不是整个 URL。

**无法打开 Mini App**：检查 BotFather 域名设置、Telegram 客户端版本、HTTPS 证书、私信中的 Web App 按钮和 Cloudflare 额外挑战规则。

**Cloudflare 验证通过但无法入群**：检查机器人管理员权限、申请是否过期、目标群是否允许入群审核，以及 Docker 日志。Turnstile 成功不等于 Telegram API 操作成功。

**域名更换**：先部署新域名并加入 Turnstile Hostname，再在后台更换 URL；建议保留旧域名至少一个完整会话有效期。

**恢复旧模式**：在后台选择数学题并独立保存；不会删除 Cloudflare 配置。
