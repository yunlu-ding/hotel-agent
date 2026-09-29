"""用于长期内存的基于 LLM 的用户偏好提取器。

职责：
- 使用 LLM 分析对话并提取结构化偏好
- 在返回新项之前，与已存储的偏好进行去重
- 保持无状态：调用者负责持久化

此模块有意地与 BaseAgent 和 MemoryManager 解耦，
以便它可以独立测试或替换为不同的提取器。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
分析以下对话，提取**跨门店、跨入住仍然成立的**住客偏好与习惯。
每条用单独一行，格式为"类别: 内容"。如果没有相关信息，就输出: 无
所有输出内容必须用中文。

【允许提取（长期有效）】
  语言: 中文
  偏好: 回答简洁
  吸烟偏好: 无烟房
  楼层偏好: 高楼层
  枕头偏好: 硬枕
  出行类型: 商务出差
  随行: 带小孩
  不喜欢: 长篇大论

【禁止提取（这些是"本次入住"的信息，不是偏好）】
  ✗ 酒店名 / 门店名 / 门店编号（如"外滩璟悦酒店""H_SH_001"）
  ✗ 房间号（如 1208）
  ✗ 订单号（如 HT1001A001）
  ✗ 入住日期 / 离店日期
  ✗ 城市（"这次住在上海"是行程，不是偏好）
  ✗ 任何与具体某次入住绑定的信息

为什么这条规则重要：这些信息一旦被当成"长期偏好"，
下一位客人（或同一位客人的下一次入住）就会拿到上一次的门店与房间，
模型会用它覆盖掉系统真正注入的会话上下文——错得很隐蔽。

对话内容：
{conversation}

提取结果（或"无"）："""

# 代码层兜底：即使模型不听话，也不让"本次入住"的信息写进长期记忆
_FORBIDDEN_CATEGORIES = (
    "酒店", "门店", "店", "房间", "房号", "住宿", "位置", "所在地",
    "订单", "入住", "离店", "城市", "地址",
)
# 注意：类别判断取的是冒号**前面**那一段，所以"偏好: 喜欢海边酒店"不会被误伤——
# 它讲的是偏好类型，不是"某一家店"。
_FORBIDDEN_VALUE_PATTERNS = (
    r"H_[A-Z]{2}_\d{3}",          # 门店编号
    r"HT\d{4}[A-Z]?\d{3}",        # 订单号
    r"\d{4}-\d{2}-\d{2}",         # 日期
    r"\d{1,2}月\d{1,2}[日号]",     # 中文日期
    # 房间号必须带上下文，否则"预算 800 元""一家三口"都会被误判（实测踩过）
    r"(房间?|房号|住在|入住)\s*[:：]?\s*\d{3,4}",
    r"\d{3,4}\s*(房|号房|房间)",
)


def is_stay_scoped(item: str) -> bool:
    """判断一条记忆是不是"本次入住"级别的信息（是则不该长期保存，也不该注入上下文）。

    写入与读取**两头都用它**：写入时拦住新的污染，读取时挡住历史上的脏数据。
    """
    import re

    category = item.split(":", 1)[0].strip()
    if any(bad in category for bad in _FORBIDDEN_CATEGORIES):
        return True
    return any(re.search(pattern, item) for pattern in _FORBIDDEN_VALUE_PATTERNS)


class PreferenceExtractor:
    """Extracts user preferences from conversation text using an LLM.

    Args:
        llm: Any LangChain-compatible chat model (``ainvoke`` required).
        max_conversation_chars: Truncate conversation input to this length
            before sending to the LLM. Keeps token usage bounded.

    Example::

        extractor = PreferenceExtractor(llm=chat_model)
        new_prefs = await extractor.extract(
            conversation_text="user: I live in Beijing...",
            existing=["city: Shanghai"],
        )
        # new_prefs -> ["city: Beijing"]
    """

    def __init__(self, llm: Any, max_conversation_chars: int = 3000) -> None:
        self._llm = llm
        self._max_chars = max_conversation_chars

    async def extract(
        self,
        conversation_text: str,
        existing: list[str] | None = None,
    ) -> list[str]:
        """Extract new preferences from conversation text.

        Args:
            conversation_text: Raw conversation (role: content lines).
            existing: Already-stored preferences for deduplication.
                      Duplicates are silently dropped.

        Returns:
            List of new preference strings in ``"category: value"`` format.
            Empty list if nothing found or on extraction failure.
        """
        truncated = conversation_text[: self._max_chars]
        logger.debug(
            "[EXTRACTOR] Input conversation (%d chars, truncated to %d):\n%s",
            len(conversation_text), len(truncated), truncated[:400],
        )
        prompt = _PROMPT_TEMPLATE.format(conversation=truncated)

        try:
            response = await self._llm.ainvoke([{"role": "user", "content": prompt}])
            raw = response.content.strip()
            logger.debug("[EXTRACTOR] LLM raw response: %s", raw)
        except Exception as exc:
            logger.warning("PreferenceExtractor LLM call failed: %s", exc)
            return []

        if not raw or raw.strip() in ("NONE", "无", "提取结果: 无", "无相关信息"):
            logger.info("[EXTRACTOR] LLM found no preferences")
            return []

        # Parse lines that look like "category: value"
        candidates = [line.strip() for line in raw.split("\n") if ":" in line]
        logger.debug("[EXTRACTOR] Parsed %d candidates: %s", len(candidates), candidates)
        if not candidates:
            return []

        filtered = self._drop_stay_scoped(candidates)
        if len(filtered) != len(candidates):
            logger.info(
                "[EXTRACTOR] 过滤掉 %d 条入住级信息（不允许进入长期记忆）",
                len(candidates) - len(filtered),
            )
        candidates = filtered

        if not existing:
            logger.info("[EXTRACTOR] No existing prefs, keeping all %d candidates", len(candidates))
            return candidates

        # Deduplicate: skip items whose text substantially overlaps existing
        existing_lower = [e.lower() for e in existing]
        new_items: list[str] = []
        for item in candidates:
            item_lower = item.lower()
            if any(item_lower in ex or ex in item_lower for ex in existing_lower):
                logger.debug("[EXTRACTOR] Duplicate skipped: %s", item)
                continue
            new_items.append(item)

        logger.info(
            "[EXTRACTOR] Result: %d new / %d skipped(dup) / %d total candidates",
            len(new_items), len(candidates) - len(new_items), len(candidates),
        )
        return new_items

    @staticmethod
    def _drop_stay_scoped(candidates: list[str]) -> list[str]:
        """丢掉"本次入住"级别的条目（见 _PROMPT_TEMPLATE 的禁止清单）。

        为什么要在代码里再拦一道：提示词是"尽力而为"，而这类污染一旦写进长期记忆，
        会持续影响之后**所有**会话，属于"错一次、脏很久"的数据。
        """
        kept: list[str] = []
        for item in candidates:
            if not is_stay_scoped(item):
                kept.append(item)
        return kept
