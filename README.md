# 🎬 astrbot_plugin_video_summary

AstrBot 视频总结插件 —— 即使视频没有字幕也能总结内容。

## 工作原理

```
视频文件 → FFmpeg 提取音频 → Cloudflare Whisper 语音转文字 → LLM 总结
```

1. **FFmpeg** 从视频中提取音频（16kHz 单声道 WAV）
2. **Cloudflare Workers AI Whisper** 将音频转为文字（支持中文、英文等多语言）
3. **LLM** 对转录文本进行智能总结

## 功能特性

- ✅ 支持无字幕视频总结
- ✅ 长视频自动分段转录
- ✅ 支持多种视频/音频格式（mp4, avi, mkv, mov, mp3, wav 等）
- ✅ Cloudflare 每天免费 ~244 分钟额度
- ✅ 可作为 LLM Tool 调用

## 前置要求

### 1. FFmpeg

需要安装 FFmpeg 并确保在系统 PATH 中可用。

**Windows 安装方式（任选其一）：**

```bash
# 方式1: pip 安装（推荐）
pip install imageio-ffmpeg

# 方式2: 手动安装
# 从 https://www.gyan.dev/ffmpeg/builds/ 下载，解压后添加到 PATH
```

### 2. Cloudflare 账号

1. 注册 [Cloudflare](https://dash.cloudflare.com/sign-up)
2. 获取 **Account ID**（Dashboard → Workers & Pages → 右侧）
3. 创建 **API Token**（[创建页面](https://dash.cloudflare.com/profile/api-tokens)）
   - 选择 "Custom token"
   - 权限: Account → Workers AI → Read
   - 复制生成的 Token

## 安装

### 方式1: AstrBot 插件管理（推荐）

在 AstrBot 管理面板中搜索 `astrbot_plugin_video_summary` 并安装。

### 方式2: 手动安装

```bash
# 克隆到 AstrBot 插件目录
cd /path/to/astrbot/plugins
git clone https://github.com/RoyougiShiki/astrbot_plugin_video_summary.git

# 安装依赖
pip install -r astrbot_plugin_video_summary/requirements.txt

# 重启 AstrBot
```

## 配置

在 AstrBot 管理面板 → 插件配置 中填写：

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `cf_account_id` | ✅ | Cloudflare Account ID |
| `cf_api_token` | ✅ | Cloudflare API Token |
| `ffmpeg_path` | ❌ | FFmpeg 路径（留空自动检测） |
| `max_audio_minutes` | ❌ | 最大音频时长（分钟），默认 30 |
| `summary_prompt` | ❌ | LLM 总结提示词，可自定义 |

## 使用

### 命令方式

```
/videosum /path/to/video.mp4
```

### LLM 工具调用

插件注册了 `video_summary` 工具，LLM 可以在对话中自动调用：

> 用户: 帮我总结一下这个视频 /path/to/video.mp4
> Bot: [自动调用 video_summary 工具进行总结]

## 支持的格式

| 类型 | 格式 |
|------|------|
| 视频 | mp4, avi, mkv, mov, wmv, flv, webm, m4v, 3gp, ts |
| 音频 | mp3, wav, flac, aac, ogg, m4a, wma, opus, amr |

## 费用说明

使用 Cloudflare Workers AI Whisper：

| 项目 | 说明 |
|------|------|
| 免费额度 | 每天 10,000 Neurons（约 244 分钟音频） |
| 超出费用 | $0.00045 / 分钟 |
| 重置时间 | 每天 00:00 UTC |

## 常见问题

### ❌ "未找到 FFmpeg"

安装 FFmpeg 或在插件配置中指定完整路径：
- Windows: `C:\ffmpeg\bin\ffmpeg.exe`
- Linux: `/usr/bin/ffmpeg`

### ❌ "Whisper API 错误"

- 检查 Account ID 和 API Token 是否正确
- 确认 API Token 有 Workers AI Read 权限
- 检查是否超出每日免费额度

### ❌ "语音识别结果为空"

- 确认视频有语音/音频内容
- 纯音乐视频可能无法识别出文字

## 技术栈

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 插件框架
- [Cloudflare Workers AI](https://ai.cloudflare.com/) - Whisper 语音识别
- [FFmpeg](https://ffmpeg.org/) - 音频提取

## 许可证

MIT License
