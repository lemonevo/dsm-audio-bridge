# DS Music Bridge

一个用 Python 标准库实现的「假 Synology Audio Station」服务端：把 WebDAV 上的音乐变成 DS Music App 能直接播放的曲库。

iPhone 上的 DS Music 只会连群晖的 Audio Station。这个桥接层实现了它需要的那部分 WebAPI，后端换成任意 WebDAV（OpenList、Alist、群晖 WebDAV 都行），于是网盘里的音乐可以直接出现在 DS Music 里，支持流式播放、封面、歌词和转码。

## 特性

- 单文件、零第三方 Python 依赖（`probe_server.py`）
- 直连 WebDAV，不依赖 rclone / FUSE 挂载
- 音频按 Range 请求流式透传，不整文件落盘
- 封面、歌词 sidecar：与音频同名的 `.jpg/.png` 和 `.lrc`；目录级 `cover.jpg` 兜底
- 依赖 ffmpeg 的格式（ape / alac / dsd / wma 等）按需转码
- 定时轻量比对重扫，另提供 `rescan.cgi` 手动触发
- 请求日志写入 `logs/requests.jsonl`，敏感参数自动脱敏

## 快速开始

```bash
cp .env.example .env   # 填 WebDAV 地址与账号
docker compose up -d --build
```

不带 Docker 直接跑（本地目录模式）：

```bash
MUSIC_DIR=~/Music PORT=5050 python3 probe_server.py
```

## 在 DS Music 里连接

- 地址：`http://<主机>:5050`（或你反向代理后的 https 地址）
- 账号密码：随意，服务端不校验；它们只用于推导固定的 `sid` 和 `SynoToken`
- 曲库名：由 `LIBRARY_NAME` 决定，默认 `Music`

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `5000` | 监听端口（Dockerfile 内为 5050） |
| `MUSIC_DIR` | `~/Music` | 本地音乐目录模式；未设置 `WEBDAV_URL` 时生效 |
| `WEBDAV_URL` | 空 | WebDAV 音乐目录，如 `http://127.0.0.1:5244/dav/Music`，设置后优先 |
| `WEBDAV_USER` / `WEBDAV_PASS` | 空 | WebDAV 账号密码 |
| `WEBDAV_TIMEOUT` | `30` | WebDAV 请求超时（秒） |
| `LIBRARY_NAME` | `Music` | DS Music 里显示的曲库名 |
| `RESCAN_INTERVAL` | `120` | 重扫间隔（秒），0 关闭 |
| `PREWARM_COVERS` / `PREWARM_ARTISTS` | `1` | 启动后预生成封面 / 歌手图 |
| `PREWARM_TRANSCODE` | `0` | 预转码热门曲目（占用磁盘，默认关） |
| `ARTIST_IMAGE_PROXY` | 空 | 歌手图抓取走哪个 HTTP 代理 |
| `COVER_SMALL_SIZE` | `600` | 小图边长（像素） |
| `CHUNK_SIZE` | `262144` | 流式传输分块大小（字节） |
| `DSM_ACCOUNT` / `DSM_PASSWORD` | `admin` / 空 | 仅用于推导 sid 与 token |

## 目录约定

```
<音乐目录>/
  某张专辑/
    01 - 曲名.flac
    01 - 曲名.lrc        # 可选，同名歌词
    01 - 曲名.jpg        # 可选，同名封面
    cover.jpg            # 可选，目录级封面兜底
```

## 注意

- 服务端**不做鉴权**（登录一律放行），**不要直接把端口暴露到公网**；需要远程访问请放在带鉴权的反向代理或内网穿透之后。
- WebDAV 后端需要支持 `Range` 请求，否则拖动进度条和流式播放会退化成整文件下载。
- 只在 DS Music（iOS）上验证过；其它 Synology 客户端不保证。