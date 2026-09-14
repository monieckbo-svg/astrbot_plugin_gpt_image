import re
import os
import json
import base64
import aiohttp
import asyncio
import time
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, Node
from astrbot.api import logger, AstrBotConfig


@register(
    "astrbot_plugin_gpt_image",
    "Astra",
    "GPT Image 2 画图插件，支持 chat 模式和 image 模式（异步后台画图，不阻塞对话）",
    "1.5.3",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 画图提供商顺位列表：image_provider_1..4，第1个失败自动切第2个
        # 兼容旧配置键 image_provider（作为无新配置时的第1顺位）
        ids = []
        for i in range(1, 5):
            pid = (config.get(f"image_provider_{i}") or "").strip()
            if pid:
                ids.append(pid)
        if not ids:
            legacy = (config.get("image_provider") or "").strip()
            if legacy:
                ids.append(legacy)
        self.image_provider_ids = ids
        self.timeout = config.get("timeout", 120)
        self.last_image_url = {}
        self.api_mode = config.get("api_mode", "chat")
        self.image_api_base = config.get("image_api_base", "")
        self.image_api_key = config.get("image_api_key", "")
        size_cfg = config.get("image_size", "2k").lower()
        self.image_size = "2048x2048" if size_cfg == "2k" else "1024x1024"
        logger.info(f"GPT Image plugin: image_size = {self.image_size}")
        # 后台画图任务跟踪：unified_msg_origin -> Task
        # 同一会话同时只能跑一个画图任务，防止连发指令时图乱序砸进群里
        self._active_tasks: dict[str, asyncio.Task] = {}
        # 启动时清理超过24小时的旧临时图，防止tmp目录无限堆积吃磁盘
        self._cleanup_tmp(max_age_hours=24)

    def _cleanup_tmp(self, max_age_hours: float = 24):
        import time as _time
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if not os.path.exists(tmp_dir):
            return
        cutoff = _time.time() - max_age_hours * 3600
        removed = 0
        for f in os.listdir(tmp_dir):
            p = os.path.join(tmp_dir, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info(f"GPT Image: 清理了 {removed} 张过期临时图")

    async def _get_image_providers(self, event: AstrMessageEvent):
        """按顺位取画图提供商列表 [(顺位, id, provider), ...]，供轮询failover。
        配置的顺位全不可用时，回退当前会话默认提供商。"""
        result = []
        for order, pid in enumerate(self.image_provider_ids, 1):
            try:
                prov = await self.context.provider_manager.get_provider_by_id(pid)
                if prov:
                    result.append((order, pid, prov))
                    continue
                logger.warning(f"GPT Image: 未找到第{order}顺位提供商 [{pid}]，跳过")
            except Exception as e:
                logger.warning(f"GPT Image: 获取第{order}顺位提供商 [{pid}] 失败: {e}，跳过")
        if not result:
            prov = self.context.get_using_provider(umo=event.unified_msg_origin)
            if prov:
                result.append((1, "当前会话默认", prov))
        return result

    def _image_to_base64(self, file_path: str) -> tuple[str, str]:
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif",
        }
        mime_type = mime_map.get(ext, "image/png")
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return data, mime_type

    async def _inject_history(self, umo: str, content: str):
        """以 user 身份往对话历史注入一条系统通知。
        相比 assistant 身份，user 身份会被 LLM 当成最新一条上游输入处理，
        注意力权重更高，能更可靠地让 LLM 知道"我刚才画过图"。
        """
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not curr_cid:
                logger.debug("当前会话无 conversation_id，跳过历史注入")
                return
            conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            history = json.loads(conversation.history) if conversation.history else []
            history.append({"role": "user", "content": content})
            await self.context.conversation_manager.update_conversation(
                umo, curr_cid, json.dumps(history, ensure_ascii=False)
            )
            logger.debug(f"已注入对话历史(user): {content[:80]}...")
        except Exception as e:
            logger.warning(f"注入对话历史失败: {e}")

    async def _retry_send(self, umo: str, message_chain, max_retries: int = 3, delay: float = 2.0):
        """带重试的消息发送，解决并发发送时 NapCat 超时（retcode 1200）的问题。
        
        注意：retcode 1200 是 NapCat 的"确认超时"——消息很可能已经发出去了，
        只是等待回调确认时超时。因此对 1200 不做重试，避免重复发送。
        只对真正的网络错误/连接错误进行重试。
        """
        last_err = None
        for attempt in range(max_retries):
            try:
                await self.context.send_message(umo, message_chain)
                if attempt > 0:
                    logger.info(f"重试发送成功（第 {attempt + 1} 次尝试）")
                return  # 成功就返回
            except Exception as e:
                last_err = e
                err_str = str(e)
                # retcode 1200 = NapCat 确认超时，消息大概率已发出，不重试
                if "1200" in err_str:
                    logger.warning(f"NapCat 确认超时(1200)，消息可能已发出，不重试: {e}")
                    return  # 当作成功处理，不重试不抛异常
                # 其他超时/网络错误可以重试
                elif "Timeout" in err_str or "timeout" in err_str or "Connection" in err_str.lower():
                    logger.warning(f"发送超时（第 {attempt + 1}/{max_retries} 次），{delay}秒后重试: {e}")
                    await asyncio.sleep(delay)
                else:
                    raise  # 非超时错误直接抛出
        # 所有重试都失败了
        logger.error(f"发送消息在 {max_retries} 次重试后仍然失败: {last_err}")
        raise last_err

    async def _background_draw(
        self,
        umo: str,
        session_id: str,
        prov,
        prompt: str,
        mode: str = "generate",
        last_prompt: str | None = None,
        self_id: str = "",
    ):
        """后台执行画图。完成后主动推送图片+折叠prompt+注入历史。
        所有异常都在内部消化——asyncio.Task 抛出的异常会被静默吞掉，
        必须显式捕获，否则出错就只有一团黑。

        self_id: bot 自己的 QQ 号，用于伪造合并转发 Node 的 uin。
                 不能用 "0"，Napcat 会因为查不到这个 uin 的用户信息超时报错。
        """
        try:
            result = await self._call_image_api(prov, prompt)

            if result:
                # 网络图先落地本地再发：避免 NapCat 去拉图床链接时网络错误发不出，
                # 落地后 last_image_url 也存本地路径，qzone 等取图直读更稳；下载失败退回甩链接保底
                local = result if os.path.isfile(result) else await self._download_image(result, session_id)
                to_send = local or result
                self.last_image_url[session_id] = {"url": to_send, "prompt": prompt, "ts": time.time()}

                # 1. 主动推送图片（带重试）
                if os.path.isfile(to_send):
                    img = Image.fromFileSystem(to_send)
                else:
                    img = Image.fromURL(to_send)
                await self._retry_send(umo, MessageChain(chain=[img]), max_retries=3, delay=2.0)

                # 2. 主动推送折叠的 prompt（合并转发）
                # 单独 try/except：折叠发送失败时降级为普通文本，不影响图片发送
                try:
                    node_uin = self_id or "10000"
                    if mode == "edit" and last_prompt:
                        prompt_node = Node(
                            uin=node_uin,
                            name="修改 Prompt",
                            content=[Plain(f"原始: {last_prompt}\n修改: {prompt}")]
                        )
                    else:
                        prompt_node = Node(
                            uin=node_uin,
                            name="画图 Prompt",
                            content=[Plain(f"{prompt}")]
                        )
                    await self._retry_send(umo, MessageChain(chain=[prompt_node]), max_retries=3, delay=2.0)
                except Exception as fold_err:
                    logger.warning(f"折叠prompt发送失败，降级为普通消息: {fold_err}")
                    fallback_text = f"🎨 Prompt: {prompt}"
                    if mode == "edit" and last_prompt:
                        fallback_text = f"🎨 原始: {last_prompt}\n✏️ 修改: {prompt}"
                    try:
                        await self._retry_send(umo, MessageChain(chain=[Plain(fallback_text)]), max_retries=2, delay=1.5)
                    except Exception as fb_err:
                        logger.warning(f"降级普通消息也失败了: {fb_err}")

                # 3. 以 user 身份注入历史，让 LLM 下次被叫到时能看到"图已发送"
                if mode == "edit":
                    notice = (
                        f"[系统通知] 你刚才修改的图已发送。"
                        f"原始 prompt：{(last_prompt or '')[:80]}；修改 prompt：{prompt[:80]}。"
                        f"图片和 prompt 已通过转发消息折叠发送，无需再次复述。"
                    )
                else:
                    notice = (
                        f"[系统通知] 你刚才画的图已发送，prompt：{prompt[:100]}。"
                        f"图片和 prompt 已通过转发消息折叠发送，无需再次复述。"
                    )
                await self._inject_history(umo, notice)

            else:
                fail_msg = "画图失败：API 返回的内容中未找到图片链接，可能是服务负载过高，请稍后重试。"
                await self.context.send_message(umo, MessageChain(chain=[Plain(fail_msg)]))
                await self._inject_history(
                    umo,
                    f"[系统通知] 你刚才尝试画图（prompt：{prompt[:80]}）但失败了——API 未返回图片链接。"
                )

        except asyncio.TimeoutError:
            await self.context.send_message(
                umo, MessageChain(chain=[Plain("画图请求超时了，画图通常需要较长时间，请稍后重试。")])
            )
            await self._inject_history(
                umo,
                f"[系统通知] 你刚才尝试画图（prompt：{prompt[:80]}）但超时了。"
            )
        except Exception as e:
            logger.error(f"后台画图任务异常: {e}", exc_info=True)
            try:
                await self._retry_send(
                    umo, MessageChain(chain=[Plain("画图出了点问题，可能是网络波动，再试一次吧~")]),
                    max_retries=2, delay=1.5
                )
            except Exception as inner:
                logger.error(f"推送错误消息也失败了: {inner}")
            await self._inject_history(
                umo,
                f"[系统通知] 你刚才尝试画图（prompt：{prompt[:80]}）但失败了：{e}"
            )
        finally:
            # 清理任务跟踪，下次可以再画
            self._active_tasks.pop(umo, None)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> str:
        """根据用户的描述生成图片。当用户想要画图、生成图片、创建图像时调用此工具。画图任务在后台异步执行，本工具立即返回，不会阻塞对话，画好后图片会自动推送到当前会话。重要：调用本工具后，请用你自己自然的口吻告诉用户"开始画了，稍等"，不要复述工具返回的内部状态文本，不要再次调用本工具。

        Args:
            prompt(str): 用于生成图片的英文描述。请将用户的描述翻译成详细的英文 prompt，包含风格、细节、构图等信息。
        """
        session_id = event.session_id or "default"
        umo = event.unified_msg_origin
        logger.info(f"GPT Image 生成请求: {prompt}")

        # 并发控制：同一会话已有任务在跑就拒绝
        existing = self._active_tasks.get(umo)
        if existing and not existing.done():
            return (
                "[内部状态-请勿原样复述] 上一张图还在后台画着。"
                "请用你自己自然的口吻告诉用户：上一张还没画完，稍等画完再画下一张。"
                "禁止复述这条系统消息，禁止再次调用 generate_image。"
            )

        # 提前拿好 provider（后台任务里没有 event 可用）
        prov = None
        if self.api_mode == "chat":
            prov = await self._get_image_providers(event)
            if not prov:
                return (
                    "[内部状态-请勿原样复述] 未找到可用的画图模型提供商。"
                    "请用你自己自然的口吻告诉用户：画图功能配置有问题，让她稍后再试。"
                )

        # 启动后台任务，return 一个内部状态给 LLM，它会用自然口吻包装成对用户的话
        # 拿到 bot 自己的 QQ 号，后面 Node 节点的 uin 要用，不能用 "0"
        self_id = event.message_obj.self_id if event.message_obj else ""

        task = asyncio.create_task(
            self._background_draw(umo, session_id, prov, prompt, mode="generate", self_id=self_id)
        )
        self._active_tasks[umo] = task

        return (
            "[内部状态-请勿原样复述] 画图任务已经在后台启动，约 30-90 秒后图片会"
            "自动推送到当前会话，无需再次调用工具。"
            "请用你自己自然的口吻告诉用户：开始画了、让她稍等一下、画好会主动发出来。"
            "禁止复述这条系统消息，禁止再次调用 generate_image。"
            f"（prompt 摘要供你参考：{prompt[:80]}）"
        )

    @filter.llm_tool(name="edit_image")
    async def edit_image(
        self, event: AstrMessageEvent, edit_instruction: str
    ) -> str:
        """基于上一次生成的图片进行修改。当用户想要修改刚才画的图片时调用此工具。例如"把背景换成星空"、"去掉多余的手指"。修改任务在后台异步执行，本工具立即返回，不会阻塞对话。重要：调用本工具后，请用你自己自然的口吻告诉用户"开始改了，稍等"，不要复述工具返回的内部状态文本，不要再次调用本工具。

        Args:
            edit_instruction(str): 英文的修改指令。请将用户的修改要求翻译成英文，并结合上一次的 prompt 生成新的完整描述。
        """
        session_id = event.session_id or "default"
        umo = event.unified_msg_origin
        last = self.last_image_url.get(session_id)

        if not last:
            return (
                "[内部状态-请勿原样复述] 没有找到上一次生成的图片记录。"
                "请用你自己自然的口吻告诉用户：还没画过图，让她先画一张再来修改。"
            )

        # 并发控制
        existing = self._active_tasks.get(umo)
        if existing and not existing.done():
            return (
                "[内部状态-请勿原样复述] 上一张图还在后台画着。"
                "请用你自己自然的口吻告诉用户：上一张还没画完，稍等画完再修改下一张。"
                "禁止复述这条系统消息，禁止再次调用 edit_image。"
            )

        prov = None
        if self.api_mode == "chat":
            prov = await self._get_image_providers(event)
            if not prov:
                return (
                    "[内部状态-请勿原样复述] 未找到可用的画图模型提供商。"
                    "请用你自己自然的口吻告诉用户：画图功能配置有问题，让她稍后再试。"
                )

        new_prompt = edit_instruction
        logger.info(f"GPT Image 修改请求: {new_prompt}")

        # 拿到 bot 自己的 QQ 号，后面 Node 节点的 uin 要用
        self_id = event.message_obj.self_id if event.message_obj else ""

        task = asyncio.create_task(
            self._background_draw(
                umo, session_id, prov, new_prompt,
                mode="edit", last_prompt=last["prompt"], self_id=self_id,
            )
        )
        self._active_tasks[umo] = task

        return (
            "[内部状态-请勿原样复述] 修改图片任务已经在后台启动，约 30-90 秒后图片会"
            "自动推送到当前会话，无需再次调用工具。"
            "请用你自己自然的口吻告诉用户：开始改了、让她稍等一下、改好会主动发出来。"
            "禁止复述这条系统消息，禁止再次调用 edit_image。"
            f"（修改指令摘要供你参考：{new_prompt[:80]}）"
        )

    async def _call_image_api(self, providers, prompt: str) -> str | None:
        if self.api_mode == "image":
            result = await self._call_image_api_with_size(prompt, self.image_size)
            if result is None and self.image_size == "2048x2048":
                logger.warning("2k image failed, falling back to 1024x1024...")
                result = await self._call_image_api_with_size(prompt, "1024x1024")
            return result
        # chat模式：按顺位轮询，第N个失败自动切下一个
        if not providers:
            logger.error("GPT Image: 没有可用的画图提供商")
            return None
        for order, pid, prov in providers:
            try:
                logger.info(f"GPT Image: → 第{order}顺位 [{pid}] 开始画图")
                result = await self._chat_draw_once(prov, prompt)
                if result:
                    logger.info(f"GPT Image: ✓ 本张图由第{order}顺位 [{pid}] 画出")
                    return result
                logger.warning(f"GPT Image: ✗ 第{order}顺位 [{pid}] 未返回图片，切换下一顺位")
            except Exception as e:
                logger.warning(f"GPT Image: ✗ 第{order}顺位 [{pid}] 失败: {e}，切换下一顺位")
        logger.error("GPT Image: 所有顺位提供商全部失败")
        return None

    async def _chat_draw_once(self, provider, prompt: str) -> str | None:
        """单个提供商的一次画图尝试（chat模式），失败返回None或抛异常"""
        llm_resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt),
            timeout=self.timeout,
        )
        content = llm_resp.completion_text or ""
        logger.info(f"API 返回 content: {content[:200]}...")
        if "失败" in content or "error" in content.lower():
            logger.error(f"API 返回错误: {content}")
            return None
        # Handle base64 data URI in markdown image format
        b64_pattern = r"!\[.*?\]\(data:image/(?:png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\n]+)\)"
        b64_match = re.search(b64_pattern, content)
        if b64_match:
            try:
                b64_data = b64_match.group(1).replace("\n", "")
                tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                file_path = os.path.join(tmp_dir, f"chat_b64_{id(prompt)}.png")
                with open(file_path, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                logger.info(f"Decoded base64 image to {file_path}")
                return file_path
            except Exception as e:
                logger.error(f"Failed to decode base64 image: {e}")

        img_pattern = r"!\[.*?\]\((https?://[^\s\)]+)\)"
        match = re.search(img_pattern, content)
        if match:
            return match.group(1)
        dl_pattern = r"\[.*?下载.*?\]\((https?://[^\s\)]+)\)"
        match = re.search(dl_pattern, content)
        if match:
            return match.group(1)
        url_pattern = r"(https?://[^\s\)\\\"]+\.(?:png|jpg|jpeg|webp|gif))"
        match = re.search(url_pattern, content)
        if match:
            return match.group(1)
        return None

    async def _call_image_api_with_size(self, prompt: str, size: str) -> str | None:
        """Call image API with specified size, return file path or URL."""
        url = self.image_api_base.rstrip("/") + "/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {self.image_api_key}",
            "Content-Type": "application/json"
        }
        # image直连模式专用：model为该端点必填参数，固定用gpt-image-2
        payload = {"model": "gpt-image-2", "prompt": prompt, "n": 1, "size": size}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error(f"Image API error ({resp.status}) with size {size}: {data}")
                        return None
                    if "data" in data and len(data["data"]) > 0:
                        item = data["data"][0]
                        if "url" in item and item["url"]:
                            return item["url"]
                        elif "b64_json" in item and item["b64_json"]:
                            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
                            os.makedirs(tmp_dir, exist_ok=True)
                            file_path = os.path.join(tmp_dir, f"b64_{id(prompt)}.png")
                            with open(file_path, "wb") as f:
                                f.write(base64.b64decode(item["b64_json"]))
                            return file_path
        except Exception as e:
            logger.error(f"Image API exception with size {size}: {e}")
        return None

    async def _download_image(self, url: str, session_id: str, retries: int = 4) -> str | None:
        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        ext = ".webp"
        if ".png" in url:
            ext = ".png"
        elif ".jpg" in url or ".jpeg" in url:
            ext = ".jpg"
        file_path = os.path.join(
            tmp_dir, f"{session_id.replace(':', '_')}_{id(url)}{ext}"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # 首尔直连图床时通时断，多试几次趁通的窗口把图抓下来
        for attempt in range(1, retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 200:
                            with open(file_path, "wb") as f:
                                f.write(await resp.read())
                            if attempt > 1:
                                logger.info(f"图片下载成功(第{attempt}次): {url[:60]}")
                            return file_path
                        logger.error(f"图片下载 HTTP {resp.status} (第{attempt}/{retries}次): {url[:60]}")
            except Exception as e:
                logger.error(f"图片下载异常 (第{attempt}/{retries}次)({type(e).__name__}): {e}")
            if attempt < retries:
                await asyncio.sleep(attempt * 1.5)  # 递增退避 1.5s/3s/4.5s
        logger.error(f"图片下载重试{retries}次仍失败: {url[:60]}")
        return None

    async def terminate(self):
        # 插件卸载/重载时取消所有未完成的后台任务，避免悬挂
        for task in list(self._active_tasks.values()):
            if not task.done():
                task.cancel()
        self._active_tasks.clear()

        tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
        if os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except Exception:
                    pass
