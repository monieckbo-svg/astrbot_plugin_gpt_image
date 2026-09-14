# astrbot_plugin_gpt_image

AstrBot 的 GPT Image 画图插件：支持 GPT Image 系列模型，多提供商按顺位容错，**后台异步画图**——工具调用立即返回，画好后图片自动推送到当前会话，不阻塞对话。

## 用法

- LLM 工具 `generate_image(prompt)`：模型根据用户描述画图，后台执行，画好主动推送。
- LLM 工具 `edit_image(...)`：基于本会话上一张图编辑。
- 每次画完，最近一张图的信息记录在 `last_image_url[session_id]` 中。

## 与 astra_qzone 的配合

`last_image_url[session_id]` 记录 `{"url", "prompt", "ts"}`，其中 **`ts` 是画图完成的时间戳**。配套的 [astrbot_plugin_astra_qzone](https://github.com/monieckbo-svg/astrbot_plugin_astra_qzone) 发说说配图时，会按 `session_id` 读取这条记录，用 `ts` 和「用户刚发进对话的图」比较新旧，实现「谁最近用谁」。

> `ts` 字段是为配合 astra_qzone 的「最新优先」取图而加。两个插件需一起安装、一起更新，否则时间比较对不上。

## 部署

在 AstrBot 插件面板用本仓库 git 地址安装。更新时建议完全卸载再重装，避免面板缓存旧文件。画图提供商、API、超时等在插件配置中设置。
