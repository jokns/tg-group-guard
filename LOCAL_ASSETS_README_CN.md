# Web 资源本地化与生产 WSGI

本包已将三处模板中的 jsDelivr CDN 引用改为 Flask `/static/vendor/` 本地路径，并将 Flask 内置开发服务器替换为 Waitress。

**注意：受当前构建环境网络限制，ZIP 不包含尚未下载的 Bootstrap / Icons 二进制资源。** 必须在服务器上运行：

```bash
cd /www/wwwroot/tgxbot/tg-group-guard-main
bash download-web-assets.sh
docker compose build --no-cache bot
docker compose up -d --force-recreate --no-deps bot
```

脚本固定版本下载 Bootstrap 5.3.0 CSS、JS 和 Bootstrap Icons 1.11.0 CSS、woff、woff2。资源下载成功后构建才会通过。不要跳过下载步骤。

部署后测试：`curl -I http://127.0.0.1:18081/static/vendor/bootstrap/css/bootstrap.min.css`，期望 HTTP 200。

本地化只针对前端运行资源；GitHub 链接仍然是普通外链。
