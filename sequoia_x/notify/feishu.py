"""飞书通知模块：将选股结果写入知识库子页面，并把页面链接推送到群聊。

新流程（替代原来的 Webhook 卡片推送）：
  1. 用应用身份（app_id / app_secret）获取 tenant_access_token；
  2. 在指定的知识库父页面下，新建一个标题为「当天日期」的子页面（doc）；
  3. 把各策略的选股结果写成文档块（含股票名 + 雪球链接）；
  4. 把子页面链接以文本消息回传到指定群聊。

依赖环境变量（见 .env.example）：
  FEISHU_APP_ID / FEISHU_APP_SECRET / WIKI_PARENT_NODE / GROUP_CHAT_ID
"""

import json
import time
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_URL = f"{_BASE}/auth/v3/tenant_access_token/internal"
_MAX_BLOCKS_PER_REQ = 50  # docx 单次创建块上限


class FeishuNotifier:
    """飞书应用身份通知器：写知识库子页面 + 回传群链接。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: str | None = None
        self._token_expire: float = 0.0
        # 汇总当日所有策略结果：(策略名, [股票代码...])
        self._results: list[tuple[str, list[str]]] = []

    # ── 鉴权 ──

    def _get_token(self) -> str | None:
        if self._token and time.time() < self._token_expire - 60:
            return self._token

        app_id = self.settings.feishu_app_id
        app_secret = self.settings.feishu_app_secret
        if not app_id or not app_secret:
            logger.error("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，无法使用知识库推送")
            return None

        try:
            resp = requests.post(
                _TOKEN_URL,
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            data = resp.json()
        except Exception as exc:
            logger.error(f"获取 tenant_access_token 失败: {exc}")
            return None

        if data.get("code") != 0:
            logger.error(f"获取 tenant_access_token 失败: {data}")
            return None

        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + int(data.get("expire", 7200))
        return self._token

    def _auth_header(self) -> dict | None:
        token = self._get_token()
        if not token:
            return None
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── 知识库 ──

    def _get_space_id(self, parent_node_token: str) -> str | None:
        url = f"{_BASE}/wiki/v2/spaces/get_node?token={parent_node_token}&token_type=wiki"
        hdr = self._auth_header()
        if not hdr:
            return None
        try:
            resp = requests.get(url, headers=hdr, timeout=10)
            data = resp.json()
        except Exception as exc:
            logger.error(f"查询知识库节点失败: {exc}")
            return None
        if data.get("code") != 0:
            logger.error(f"查询知识库节点失败: {data}")
            return None
        # Feishu 返回结构：data.data.node.space_id
        node = (data.get("data") or {}).get("node") or {}
        space_id = node.get("space_id")
        if not space_id:
            logger.error(f"知识库节点响应缺少 space_id: {data}")
            return None
        return space_id

    def _create_child_node(self, space_id: str, parent_node_token: str, title: str) -> dict | None:
        url = f"{_BASE}/wiki/v2/spaces/{space_id}/nodes"
        hdr = self._auth_header()
        if not hdr:
            return None
        # node_type=origin 表示在知识库内新建真实页面；obj_type=docx 表示页面对象为飞书文档
        body = {
            "parent_node_token": parent_node_token,
            "node_type": "origin",
            "obj_type": "docx",
            "title": title,
        }
        try:
            resp = requests.post(url, headers=hdr, json=body, timeout=10)
            data = resp.json()
        except Exception as exc:
            logger.error(f"创建知识库子节点失败: {exc}")
            return None
        if data.get("code") != 0:
            logger.error(f"创建知识库子节点失败: {data}")
            return None
        return (data.get("data") or {}).get("node")

    def _create_daily_page(self, date_str: str) -> dict | None:
        parent = self.settings.wiki_parent_node
        if not parent:
            logger.error("缺少 WIKI_PARENT_NODE，无法创建子页面")
            return None
        space_id = self._get_space_id(parent)
        if not space_id:
            return None
        logger.info(f"在知识库父页面下创建子页面：{date_str}")
        return self._create_child_node(space_id, parent, date_str)

    # ── 股票名 / 雪球链接（复用 baostock 查名） ──

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。"""
        import baostock as bs

        mapping: dict[str, str] = {}
        if not symbols:
            return mapping
        bs.login()
        try:
            for code in symbols:
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                rs = bs.query_stock_basic(code=f"{prefix}.{code}")
                while rs.next():
                    row = rs.get_row_data()
                    mapping[code] = row[1]  # 第2个字段是股票名称
        finally:
            bs.logout()
        return mapping

    # ── 文档内容 ──

    def _build_blocks(self, date_str: str) -> list[dict]:
        all_symbols = [s for _, syms in self._results for s in syms]
        names = self._get_stock_names(all_symbols)

        blocks: list[dict] = [
            {
                "block_type": 3,  # heading1
                "heading1": {
                    "elements": [{"text_run": {"content": f"Sequoia-X 选股播报 {date_str}"}}]
                },
            }
        ]

        for strategy_name, symbols in self._results:
            blocks.append(
                {
                    "block_type": 4,  # heading2
                    "heading2": {
                        "elements": [
                            {"text_run": {"content": f"{strategy_name}（{len(symbols)} 只）"}}
                        ]
                    },
                }
            )
            if not symbols:
                blocks.append(
                    {
                        "block_type": 2,  # text
                        "text": {"elements": [{"text_run": {"content": "（无选股结果）"}}]},
                    }
                )
                continue
            for code in symbols:
                name = names.get(code, code)
                xq = self._to_xueqiu_code(code)
                blocks.append(
                    {
                        "block_type": 2,  # text（段落，含超链接）
                        "text": {
                            "elements": [
                                {
                                    "text_run": {
                                        "content": f"{name} ({code})",
                                        "text_element_style": {
                                            "url": f"https://xueqiu.com/S/{xq}"
                                        },
                                    }
                                }
                            ]
                        },
                    }
                )
        return blocks

    def _write_doc(self, document_id: str, blocks: list[dict]) -> None:
        hdr = self._auth_header()
        if not hdr:
            return
        url = (
            f"{_BASE}/docx/v1/documents/{document_id}/blocks/{document_id}"
            f"/children?document_revision_id=-1"
        )
        for i in range(0, len(blocks), _MAX_BLOCKS_PER_REQ):
            batch = blocks[i : i + _MAX_BLOCKS_PER_REQ]
            body = {"index": -1, "children": batch}  # index=-1 追加到末尾
            try:
                resp = requests.post(url, headers=hdr, json=body, timeout=15)
                data = resp.json()
            except Exception as exc:
                logger.error(f"写入文档块失败: {exc}")
                return
            if data.get("code") != 0:
                logger.error(f"写入文档块失败: {data}")
                return
        logger.info(f"文档内容写入完成：{document_id}（{len(blocks)} 块）")

    # ── 群消息 ──

    def _send_group_message(self, text: str) -> None:
        chat_id = self.settings.group_chat_id
        hdr = self._auth_header()
        if not hdr or not chat_id:
            logger.error("缺少 GROUP_CHAT_ID 或鉴权失败，无法发送群消息")
            return
        url = f"{_BASE}/im/v1/messages?receive_id_type=chat_id"
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        try:
            resp = requests.post(url, headers=hdr, json=body, timeout=10)
            data = resp.json()
        except Exception as exc:
            logger.error(f"发送群消息失败: {exc}")
            return
        if data.get("code") != 0:
            logger.error(f"发送群消息失败: {data}")
            return
        logger.info("群消息发送成功")

    # ── 对外接口 ──

    def send(self, strategy_name: str, symbols: list[str]) -> None:
        """汇总单个策略的选股结果（不直接推送）。"""
        self._results.append((strategy_name, list(symbols)))

    def publish(self) -> None:
        """收尾：创建当日知识库子页面、写入结果、把链接发到群。"""
        date_str = date.today().strftime("%Y-%m-%d")
        if not self._results:
            logger.info("无选股结果，跳过知识库推送")
            return

        node = self._create_daily_page(date_str)
        if not node:
            logger.error("创建知识库子页面失败，终止推送")
            return

        document_id = node.get("obj_token") or node.get("document_id")
        page_url = node.get("url")
        if not document_id:
            logger.error(f"子页面缺少 document_id，无法写入内容: {node}")
            return

        blocks = self._build_blocks(date_str)
        self._write_doc(document_id, blocks)

        # 无论文档写入是否完整，子页面已创建，均把链接回传群
        text = f"📈 Sequoia-X 选股播报 {date_str}\n📄 知识库：{page_url or document_id}"
        self._send_group_message(text)
