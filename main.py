"""
Textworld 插件 - 多房间文字冒险游戏
v2.6.0 - 配置规范化
"""

import asyncio
import os
import tempfile
import httpx
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import MessageChain

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False


# ==================== 数据模型 ====================

class RoomStatus(Enum):
    WAITING = "waiting"
    CHARACTER_CREATION = "creating"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class PlayerStatus(Enum):
    ACTIVE = "active"
    PENDING = "pending"
    TIMEOUT = "timeout"
    ACTED = "acted"
    CREATING_CHAR = "creating"
    CHAR_DONE = "char_done"


class CreationStep(Enum):
    ROOM_NAME = "room_name"
    TIMEOUT = "timeout"
    WORLD_SETTING = "world_setting"
    WORLD_TOO_LONG = "world_too_long"
    SUMMARIZING = "summarizing"
    CONFIRM = "confirm"


@dataclass
class PendingCreation:
    player_id: str
    player_name: str
    player_umo: str
    step: CreationStep = CreationStep.ROOM_NAME
    room_name: Optional[str] = None
    timeout: Optional[int] = None
    world_setting: Optional[str] = None
    original_world_setting: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class Player:
    player_id: str
    player_name: str
    unified_msg_origin: str
    character_name: Optional[str] = None
    character_setting: Optional[str] = None
    status: PlayerStatus = PlayerStatus.ACTIVE
    join_time: float = field(default_factory=time.time)
    last_action_time: Optional[float] = None
    current_action: Optional[str] = None

    def reset_for_new_round(self):
        self.status = PlayerStatus.ACTIVE
        self.current_action = None
    
    def has_character(self) -> bool:
        return self.character_name is not None and self.character_setting is not None


@dataclass
class PendingConfig:
    timeout: Optional[int] = None
    correction_text: Optional[str] = None


@dataclass
class GameHistory:
    round_number: int
    player_actions: Dict[str, str]
    dm_response: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Room:
    room_id: str
    room_name: str
    host_id: str
    host_umo: str
    world_setting: str
    original_world_setting: Optional[str] = None
    
    status: RoomStatus = RoomStatus.WAITING
    paused: bool = False
    
    active_players: Dict[str, Player] = field(default_factory=dict)
    pending_players: Dict[str, Player] = field(default_factory=dict)
    
    timeout: int = 300
    char_creation_timeout: int = 180
    pending_config: PendingConfig = field(default_factory=PendingConfig)
    
    current_round: int = 0
    round_start_time: Optional[float] = None
    char_creation_start_time: Optional[float] = None
    history: List[GameHistory] = field(default_factory=list)
    
    created_at: float = field(default_factory=time.time)
    
    def get_all_players(self) -> List[Player]:
        return list(self.active_players.values()) + list(self.pending_players.values())
    
    def get_unique_origins(self) -> Set[str]:
        return {p.unified_msg_origin for p in self.get_all_players()}
    
    def get_active_player_count(self) -> int:
        return len(self.active_players)
    
    def is_host(self, player_id: str) -> bool:
        return player_id == self.host_id
    
    def activate_pending_players(self):
        for player_id, player in self.pending_players.items():
            player.status = PlayerStatus.ACTIVE
            self.active_players[player_id] = player
        self.pending_players.clear()
    
    def apply_pending_config(self):
        if self.pending_config.timeout is not None:
            self.timeout = self.pending_config.timeout
    
    def start_character_creation(self):
        self.status = RoomStatus.CHARACTER_CREATION
        self.char_creation_start_time = time.time()
        for player in self.active_players.values():
            player.status = PlayerStatus.CREATING_CHAR
    
    def check_all_characters_done(self) -> bool:
        return all(p.status == PlayerStatus.CHAR_DONE for p in self.active_players.values())
    
    def start_new_round(self):
        self.current_round += 1
        self.round_start_time = time.time()
        for player in self.active_players.values():
            player.reset_for_new_round()
    
    def check_all_players_acted(self) -> bool:
        return all(p.status != PlayerStatus.ACTIVE for p in self.active_players.values())
    
    def check_all_players_timeout(self) -> bool:
        return all(p.status == PlayerStatus.TIMEOUT for p in self.active_players.values())
    
    def get_round_actions(self) -> Dict[str, str]:
        return {
            p.character_name or p.player_name: p.current_action 
            for p in self.active_players.values() 
            if p.current_action
        }
    
    def get_characters_info(self) -> str:
        lines = []
        for p in self.active_players.values():
            if p.has_character():
                lines.append(f"【{p.character_name}】\n{p.character_setting}")
        return "\n\n".join(lines) if lines else "无角色信息"
    
    def build_game_context(self, history_rounds: int = 5) -> str:
        parts = [f"【世界观设定】\n{self.world_setting}"]
        
        chars = self.get_characters_info()
        if chars != "无角色信息":
            parts.append(f"\n【角色信息】\n{chars}")
        
        if self.pending_config.correction_text:
            parts.append(f"\n【房主补充】\n{self.pending_config.correction_text}")
        
        if self.history:
            parts.append("\n【历史记录】")
            for h in self.history[-history_rounds:]:
                parts.append(f"\n第{h.round_number}轮:")
                for name, action in h.player_actions.items():
                    parts.append(f"  - {name}: {action}")
                preview = h.dm_response[:100] + "..." if len(h.dm_response) > 100 else h.dm_response
                parts.append(f"  DM: {preview}")
        
        return "\n".join(parts)


# ==================== 房间管理器 ====================

class RoomManager:
    def __init__(self, max_rooms: int = 10):
        self.rooms: Dict[str, Room] = {}
        self.player_room_map: Dict[str, str] = {}
        self.max_rooms = max_rooms
    
    def can_create_room(self) -> bool:
        return len(self.rooms) < self.max_rooms
    
    def create_room(self, host_id: str, host_name: str, host_umo: str,
                    room_name: str, world_setting: str, timeout: int = 300,
                    char_timeout: int = 180,
                    original_world_setting: Optional[str] = None) -> Optional[Room]:
        if not self.can_create_room() or host_id in self.player_room_map:
            return None
        
        room_id = str(uuid.uuid4())[:8]
        host_player = Player(player_id=host_id, player_name=host_name, unified_msg_origin=host_umo)
        
        room = Room(
            room_id=room_id, room_name=room_name, host_id=host_id, host_umo=host_umo,
            world_setting=world_setting, original_world_setting=original_world_setting,
            timeout=timeout, char_creation_timeout=char_timeout,
            active_players={host_id: host_player}
        )
        
        self.rooms[room_id] = room
        self.player_room_map[host_id] = room_id
        return room
    
    def get_room(self, room_id: str) -> Optional[Room]:
        return self.rooms.get(room_id)
    
    def get_room_by_player(self, player_id: str) -> Optional[Room]:
        room_id = self.player_room_map.get(player_id)
        return self.rooms.get(room_id) if room_id else None
    
    def join_room(self, room_id: str, player_id: str, player_name: str,
                  player_umo: str, max_players: int = 8) -> tuple[bool, str]:
        room = self.get_room(room_id)
        if not room:
            return False, "房间不存在"
        if room.status == RoomStatus.CLOSED:
            return False, "房间已关闭"
        if room.status in [RoomStatus.CHARACTER_CREATION, RoomStatus.ACTIVE]:
            return False, "游戏已开始"
        if player_id in self.player_room_map:
            return False, "已在其他房间"
        if room.get_active_player_count() + len(room.pending_players) >= max_players:
            return False, "房间已满"
        
        player = Player(player_id=player_id, player_name=player_name, unified_msg_origin=player_umo)
        
        if room.paused:
            player.status = PlayerStatus.PENDING
            room.pending_players[player_id] = player
        else:
            room.active_players[player_id] = player
        
        self.player_room_map[player_id] = room_id
        return True, "已加入"
    
    def leave_room(self, player_id: str) -> tuple[bool, str]:
        room = self.get_room_by_player(player_id)
        if not room:
            return False, "不在房间中"
        
        room.active_players.pop(player_id, None)
        room.pending_players.pop(player_id, None)
        del self.player_room_map[player_id]
        
        if player_id == room.host_id:
            self.close_room(room.room_id)
            return True, "房主离开，房间关闭"
        return True, "已离开"
    
    def close_room(self, room_id: str) -> bool:
        room = self.get_room(room_id)
        if not room:
            return False
        
        room.status = RoomStatus.CLOSED
        for player in room.get_all_players():
            self.player_room_map.pop(player.player_id, None)
        del self.rooms[room_id]
        return True
    
    def get_all_rooms(self) -> List[Room]:
        return list(self.rooms.values())
    
    def pause_room(self, room_id: str, player_id: str) -> tuple[bool, str]:
        room = self.get_room(room_id)
        if not room:
            return False, "房间不存在"
        if not room.is_host(player_id):
            return False, "非房主"
        if room.paused:
            return False, "已暂停"
        
        room.paused = True
        room.status = RoomStatus.PAUSED
        return True, "已暂停"
    
    def resume_room(self, room_id: str, player_id: str) -> tuple[bool, str]:
        room = self.get_room(room_id)
        if not room:
            return False, "房间不存在"
        if not room.is_host(player_id):
            return False, "非房主"
        if not room.paused:
            return False, "未暂停"
        
        room.apply_pending_config()
        room.activate_pending_players()
        room.paused = False
        room.status = RoomStatus.ACTIVE
        return True, "已恢复"


# ==================== 文件解析器 ====================

class FileParser:
    SUPPORTED = ['.txt', '.docx']
    
    @classmethod
    async def download_file(cls, url: str, timeout: int = 30) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                return resp.content if resp.status_code == 200 else None
        except Exception as e:
            logger.error(f"下载失败: {e}")
            return None
    
    @classmethod
    def parse_txt(cls, content: bytes) -> tuple[bool, str]:
        for enc in ['utf-8', 'gbk', 'gb2312', 'utf-16', 'latin-1']:
            try:
                text = content.decode(enc).strip()
                if text:
                    return True, text
            except:
                continue
        return False, "无法识别编码"
    
    @classmethod
    def parse_docx(cls, content: bytes) -> tuple[bool, str]:
        if not DOCX_AVAILABLE:
            return False, "请安装 python-docx"
        
        try:
            with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
                f.write(content)
                tmp_path = f.name
            
            try:
                doc = Document(tmp_path)
                paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                return (True, "\n\n".join(paragraphs)) if paragraphs else (False, "文档为空")
            finally:
                os.unlink(tmp_path)
        except Exception as e:
            return False, f"解析失败: {e}"
    
    @classmethod
    async def parse_file(cls, url: str, filename: str) -> tuple[bool, str]:
        ext = os.path.splitext(filename.lower())[1]
        if ext not in cls.SUPPORTED:
            return False, "不支持的格式"
        
        content = await cls.download_file(url)
        if not content:
            return False, "下载失败"
        
        if ext == '.txt':
            return cls.parse_txt(content)
        elif ext == '.docx':
            return cls.parse_docx(content)
        return False, "未知错误"


# ==================== 主插件类 ====================

@register(
    "textworld",
    "YourName", 
    "多房间文字冒险游戏插件",
    "2.6.0",
    "https://github.com/yourname/astrbot_plugin_textworld"
)
class TextworldPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 从配置读取参数
        self.max_rooms = config.get("max_rooms", 10)
        self.default_timeout = config.get("default_timeout", 300)
        self.char_creation_timeout = config.get("char_creation_timeout", 180)
        self.max_players = config.get("max_players_per_room", 8)
        self.creation_timeout = config.get("creation_timeout", 300)
        
        # 管理员
        self.admin_ids: List[str] = config.get("admin_ids", [])
        
        # 世界观配置
        self.world_setting_max_length = config.get("world_setting_max_length", 4000)
        self.world_setting_summary_length = config.get("world_setting_summary_length", 2000)
        self.world_template = config.get("world_template", "")
        
        # 消息配置
        self.chunk_size = config.get("chunk_size", 1000)
        
        # AI配置
        self.opening_max_length = config.get("opening_max_length", 400)
        self.dm_response_max_length = config.get("dm_response_max_length", 500)
        self.history_rounds = config.get("history_rounds_in_context", 5)
        self.character_setting_max_length = config.get("character_setting_max_length", 500)
        self.dm_style = config.get("dm_style", "生动、富有画面感、适度描写细节")
        
        # 初始化房间管理器
        self.room_manager = RoomManager(self.max_rooms)
        
        # 内部状态
        self.timeout_tasks: Dict[str, asyncio.Task] = {}
        self.pending_creations: Dict[str, PendingCreation] = {}
        
        logger.info(f"Textworld v2.6.0 已加载")
    
    def _is_admin(self, player_id: str) -> bool:
        return player_id in self.admin_ids
    
    # ==================== 消息处理 ====================
    
    def _split_text(self, text: str) -> List[str]:
        """分割长文本"""
        chunks: List[str] = []
        paragraphs = text.split('\n')
        current = ""
        
        for para in paragraphs:
            if len(current) + len(para) + 1 <= self.chunk_size:
                current += ("\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                if len(para) > self.chunk_size:
                    for i in range(0, len(para), self.chunk_size):
                        chunks.append(para[i:i+self.chunk_size])
                    current = ""
                else:
                    current = para
        
        if current:
            chunks.append(current)
        
        return chunks if chunks else [text]
    
    def _build_long_message(self, text: str, title: Optional[str] = None) -> str:
        """构建长消息"""
        chunks = self._split_text(text)
        
        result = ""
        if title:
            result = f"━━━━ {title} ━━━━\n\n"
        
        if len(chunks) == 1:
            result += chunks[0]
        else:
            for i, chunk in enumerate(chunks):
                result += f"[第{i+1}部分/{len(chunks)}]\n{chunk}\n\n"
        
        return result.strip()
    
    def _send_long_message(self, event: AstrMessageEvent, text: str, 
                           title: Optional[str] = None) -> MessageEventResult:
        """发送长消息"""
        message = self._build_long_message(text, title)
        return event.plain_result(message)
    
    # ==================== 广播消息 ====================
    
    async def _broadcast(self, room: Room, message: str):
        """广播消息（按来源去重）"""
        unique_origins = room.get_unique_origins()
        chain = MessageChain().message(message)
        
        for origin in unique_origins:
            try:
                await self.context.send_message(origin, chain)
            except Exception as e:
                logger.error(f"广播失败 {origin}: {e}")
    
    async def _broadcast_long(self, room: Room, text: str, 
                               title: Optional[str] = None,
                               footer: Optional[str] = None):
        """广播长消息"""
        message = ""
        if title:
            message = f"━━━━ {title} ━━━━\n\n"
        
        chunks = self._split_text(text)
        if len(chunks) == 1:
            message += chunks[0]
        else:
            for i, chunk in enumerate(chunks):
                message += f"[第{i+1}部分/{len(chunks)}]\n{chunk}\n\n"
        
        if footer:
            message += f"\n━━━━━━━━━━━━━━━━\n{footer}"
        
        await self._broadcast(room, message.strip())
    
    # ==================== 文件处理 ====================
    
    def _extract_file_from_event(self, event: AstrMessageEvent) -> Optional[Dict[str, str]]:
        try:
            message = event.message_obj
            if hasattr(message, 'message') and message.message:
                for comp in message.message:
                    comp_type = type(comp).__name__.lower()
                    if 'file' in comp_type:
                        url = getattr(comp, 'url', None) or getattr(comp, 'file', None)
                        name = getattr(comp, 'name', None) or getattr(comp, 'filename', 'file')
                        if url:
                            return {"url": url, "filename": name}
            return None
        except:
            return None
    
    async def _handle_file_upload(self, file_info: Dict[str, str]) -> tuple[bool, str, str]:
        url = file_info.get("url", "")
        filename = file_info.get("filename", "unknown")
        
        if not url:
            return False, "无法获取URL", filename
        
        success, result = await FileParser.parse_file(url, filename)
        return success, result, filename
    
    # ==================== 世界观处理 ====================
    
    async def _summarize_world_setting(self, player_umo: str, text: str) -> tuple[bool, str, Optional[str]]:
        """AI 总结世界观"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(player_umo)
            if not provider_id:
                return False, text[:self.world_setting_max_length], "无AI服务"
            
            prompt = f"""请将以下世界观设定精简总结为{self.world_setting_summary_length}字以内，保留核心设定：

{text}"""
            
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            
            if resp and resp.completion_text and len(resp.completion_text.strip()) > 50:
                return True, resp.completion_text.strip(), None
            
            return False, text[:self.world_setting_max_length], "AI总结失败"
        except Exception as e:
            return False, text[:self.world_setting_max_length], str(e)[:20]
    
    # ==================== 消息监听 ====================
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听消息"""
        player_id = event.get_sender_id()
        text = event.message_str.strip()
        
        if text.startswith("/"):
            return
        
        # 处理房间创建流程
        if player_id in self.pending_creations:
            pending = self.pending_creations[player_id]
            
            if time.time() - pending.created_at > self.creation_timeout:
                del self.pending_creations[player_id]
                yield event.plain_result("⏰ 创建超时")
                return
            
            if pending.step == CreationStep.SUMMARIZING:
                yield event.plain_result("⏳ AI处理中，请稍候...")
                return
            
            if pending.step == CreationStep.ROOM_NAME:
                yield self._handle_room_name(event, pending, text)
            
            elif pending.step == CreationStep.TIMEOUT:
                yield self._handle_timeout(event, pending, text)
            
            elif pending.step == CreationStep.WORLD_SETTING:
                file_info = self._extract_file_from_event(event)
                if file_info:
                    yield event.plain_result(f"📄 解析中...")
                    success, content, _ = await self._handle_file_upload(file_info)
                    if not success:
                        yield event.plain_result(f"❌ {content}")
                        return
                    yield event.plain_result(f"✅ 解析成功，{len(content)}字")
                    text = content
                
                yield self._handle_world_setting(event, pending, text)
            
            elif pending.step == CreationStep.WORLD_TOO_LONG:
                results = await self._handle_world_too_long_choice(event, pending, text)
                for r in results:
                    yield r
            
            elif pending.step == CreationStep.CONFIRM:
                yield self._handle_confirm(event, pending, text)
            
            return
        
        # 处理角色创建
        room = self.room_manager.get_room_by_player(player_id)
        if room and room.status == RoomStatus.CHARACTER_CREATION:
            player = room.active_players.get(player_id)
            if player and player.status == PlayerStatus.CREATING_CHAR:
                yield await self._handle_character_input(event, room, player, text)
    
    # ==================== 房间创建流程 ====================
    
    def _handle_room_name(self, event: AstrMessageEvent, pending: PendingCreation, text: str) -> MessageEventResult:
        if len(text) < 1 or len(text) > 30:
            return event.plain_result("❌ 房间名称应为 1-30 字符")
        
        pending.room_name = text
        pending.step = CreationStep.TIMEOUT
        
        return event.plain_result(
            f"✅ 名称: {text}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏱️ 请输入回合超时时间（30-600秒）\n"
            f"💡 输入 '默认' = {self.default_timeout}秒"
        )
    
    def _handle_timeout(self, event: AstrMessageEvent, pending: PendingCreation, text: str) -> MessageEventResult:
        if text in ["默认", "default"]:
            pending.timeout = self.default_timeout
        else:
            try:
                t = int(text)
                if not 30 <= t <= 600:
                    return event.plain_result("❌ 请输入 30-600 之间的数字")
                pending.timeout = t
            except:
                return event.plain_result("❌ 请输入数字或 '默认'")
        
        pending.step = CreationStep.WORLD_SETTING
        
        return event.plain_result(
            f"✅ 超时: {pending.timeout}秒\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🌍 请输入世界观设定\n"
            f"📝 支持：直接输入 / 上传 .txt / .docx\n"
            f"💡 建议不超过 {self.world_setting_max_length} 字"
        )
    
    def _handle_world_setting(self, event: AstrMessageEvent, pending: PendingCreation, text: str) -> MessageEventResult:
        """处理世界观输入"""
        if text in ["默认", "default"] and self.world_template:
            pending.world_setting = self.world_template
            pending.step = CreationStep.CONFIRM
            return self._show_confirm(event, pending)
        
        if len(text) < 10:
            return event.plain_result("❌ 世界观至少需要 10 个字")
        
        if len(text) > self.world_setting_max_length:
            pending.original_world_setting = text
            pending.step = CreationStep.WORLD_TOO_LONG
            
            return event.plain_result(
                f"⚠️ 世界观过长！\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📊 当前: {len(text)} 字\n"
                f"📊 建议: ≤ {self.world_setting_max_length} 字\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"请选择处理方式：\n\n"
                f"1️⃣ 输入 '总结' → AI总结为 ~{self.world_setting_summary_length}字\n"
                f"2️⃣ 输入 '截断' → 保留前 {self.world_setting_max_length}字\n"
                f"3️⃣ 输入 '保留' → 使用全文（可能影响AI效果）\n"
                f"4️⃣ 重新输入更短的世界观"
            )
        
        pending.world_setting = text
        pending.step = CreationStep.CONFIRM
        return self._show_confirm(event, pending)
    
    async def _handle_world_too_long_choice(self, event: AstrMessageEvent, 
                                             pending: PendingCreation, 
                                             text: str) -> List[MessageEventResult]:
        """处理世界观过长时的用户选择"""
        results: List[MessageEventResult] = []
        choice = text.lower().strip()
        
        original = pending.original_world_setting or ""
        
        if choice in ["总结", "1", "ai", "summary"]:
            pending.step = CreationStep.SUMMARIZING
            results.append(event.plain_result(f"⏳ AI正在总结 {len(original)} 字..."))
            
            success, summary, err = await self._summarize_world_setting(pending.player_umo, original)
            
            if success:
                pending.world_setting = summary
                pending.step = CreationStep.CONFIRM
                results.append(event.plain_result(f"✅ 总结完成: {len(original)} → {len(summary)} 字"))
                results.append(self._show_confirm(event, pending))
            else:
                pending.step = CreationStep.WORLD_TOO_LONG
                results.append(event.plain_result(f"❌ 总结失败: {err}\n请重新选择：总结 / 截断 / 保留"))
        
        elif choice in ["截断", "2", "cut", "truncate"]:
            pending.world_setting = original[:self.world_setting_max_length]
            pending.step = CreationStep.CONFIRM
            results.append(event.plain_result(f"✅ 已截断为前 {self.world_setting_max_length} 字"))
            results.append(self._show_confirm(event, pending))
        
        elif choice in ["保留", "3", "keep", "full"]:
            pending.world_setting = original
            pending.step = CreationStep.CONFIRM
            results.append(event.plain_result(f"✅ 保留全部 {len(original)} 字"))
            results.append(self._show_confirm(event, pending))
        
        else:
            if len(text) >= 10:
                if len(text) <= self.world_setting_max_length:
                    pending.world_setting = text
                    pending.original_world_setting = None
                    pending.step = CreationStep.CONFIRM
                    results.append(event.plain_result(f"✅ 新世界观已保存 ({len(text)}字)"))
                    results.append(self._show_confirm(event, pending))
                else:
                    pending.original_world_setting = text
                    results.append(event.plain_result(f"⚠️ 仍然过长 ({len(text)}字)\n请选择：总结 / 截断 / 保留"))
            else:
                results.append(event.plain_result(
                    "❓ 请选择：\n"
                    "• 总结 - AI总结\n"
                    "• 截断 - 保留前部分\n"
                    "• 保留 - 使用全文\n"
                    "• 或输入新的世界观（≥10字）"
                ))
        
        return results
    
    def _show_confirm(self, event: AstrMessageEvent, pending: PendingCreation) -> MessageEventResult:
        """显示确认信息"""
        world = pending.world_setting or ""
        preview = world[:200] + "..." if len(world) > 200 else world
        
        original_info = ""
        if pending.original_world_setting and len(pending.original_world_setting) != len(world):
            original_info = f"\n📊 原文 {len(pending.original_world_setting)} → 当前 {len(world)} 字"
        
        return event.plain_result(
            f"📋 请确认房间配置\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📍 名称: {pending.room_name}\n"
            f"⏱️ 超时: {pending.timeout}秒\n"
            f"🌍 世界观: {len(world)}字{original_info}\n\n"
            f"{preview}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"输入: 确认 | 取消 | 重来 | 查看完整"
        )
    
    def _handle_confirm(self, event: AstrMessageEvent, pending: PendingCreation, text: str) -> MessageEventResult:
        t = text.lower().strip()
        
        if t in ["查看完整", "查看", "full", "view", "完整"]:
            world = pending.world_setting or ""
            return self._send_long_message(event, world, title=f"完整世界观 ({len(world)}字)")
        
        if t in ["确认", "y", "yes", "ok", "确定"]:
            room = self.room_manager.create_room(
                host_id=pending.player_id,
                host_name=pending.player_name,
                host_umo=pending.player_umo,
                room_name=pending.room_name or "冒险",
                world_setting=pending.world_setting or "",
                timeout=pending.timeout or self.default_timeout,
                char_timeout=self.char_creation_timeout,
                original_world_setting=pending.original_world_setting
            )
            
            del self.pending_creations[pending.player_id]
            
            if not room:
                return event.plain_result("❌ 创建失败")
            
            return event.plain_result(
                f"🎮 房间创建成功！\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📍 名称: {room.room_name}\n"
                f"🆔 ID: {room.room_id}\n"
                f"⏱️ 超时: {room.timeout}秒\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📢 邀请: /tw join {room.room_id}\n"
                f"👉 开始: /tw begin"
            )
        
        if t in ["取消", "n", "no", "cancel"]:
            del self.pending_creations[pending.player_id]
            return event.plain_result("❌ 已取消创建")
        
        if t in ["重来", "restart", "reset"]:
            pending.step = CreationStep.ROOM_NAME
            pending.room_name = None
            pending.timeout = None
            pending.world_setting = None
            pending.original_world_setting = None
            pending.created_at = time.time()
            return event.plain_result("🔄 重新开始\n📝 请输入房间名称（1-30字）:")
        
        return event.plain_result("❓ 请输入: 确认 | 取消 | 重来 | 查看完整")
    
    # ==================== 角色创建 ====================
    
    async def _handle_character_input(self, event: AstrMessageEvent, room: Room, 
                                        player: Player, text: str) -> MessageEventResult:
        """处理角色设定"""
        if "：" in text:
            parts = text.split("：", 1)
        elif ":" in text:
            parts = text.split(":", 1)
        elif "\n" in text:
            parts = text.split("\n", 1)
        else:
            return event.plain_result(
                "❌ 格式错误\n"
                "请使用: 角色名：角色设定\n"
                "或: 角色名\\n角色设定"
            )
        
        char_name = parts[0].strip()
        char_setting = parts[1].strip() if len(parts) > 1 else ""
        
        if len(char_name) < 1 or len(char_name) > 20:
            return event.plain_result("❌ 角色名 1-20 字")
        
        if len(char_setting) < 5:
            return event.plain_result("❌ 角色设定至少 5 字")
        
        if len(char_setting) > self.character_setting_max_length:
            char_setting = char_setting[:self.character_setting_max_length] + "..."
        
        player.character_name = char_name
        player.character_setting = char_setting
        player.status = PlayerStatus.CHAR_DONE
        
        await self._broadcast(room, f"✅ {player.player_name} → 【{char_name}】")
        
        if room.check_all_characters_done():
            await self._stop_timeout(f"char_{room.room_id}")
            await self._start_game_after_characters(room)
        
        return event.plain_result(
            f"✅ 角色创建完成！\n"
            f"👤 {char_name}\n"
            f"📝 {char_setting[:80]}{'...' if len(char_setting) > 80 else ''}"
        )
    
    async def _start_game_after_characters(self, room: Room):
        """角色创建完成后开始游戏"""
        room.status = RoomStatus.ACTIVE
        room.start_new_round()
        
        opening = await self._generate_opening(room)
        
        char_intro = "【参与角色】\n"
        for p in room.active_players.values():
            char_intro += f"• {p.character_name}（{p.player_name}）\n"
        
        message = (
            f"━━━━ 🎭 {room.room_name} 开始！ ━━━━\n\n"
            f"{char_intro}\n"
            f"【开场】\n{opening}\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🔄 第1轮 | ⏱️{room.timeout}秒\n"
            f"使用 /tw act <行动描述> 进行冒险"
        )
        
        await self._broadcast(room, message)
        await self._start_timeout(room)
    
    # ==================== 命令处理 ====================
    
    @filter.command_group("tw")
    def tw(self):
        pass
    
    @tw.command("start")
    async def cmd_start(self, event: AstrMessageEvent):
        """创建房间"""
        player_id = event.get_sender_id()
        
        if self.room_manager.get_room_by_player(player_id):
            yield event.plain_result("❌ 你已在房间中，请先 /tw leave")
            return
        
        if player_id in self.pending_creations:
            yield event.plain_result("⚠️ 正在创建中，/tw cancel 取消")
            return
        
        if not self.room_manager.can_create_room():
            yield event.plain_result("❌ 房间数量已满")
            return
        
        self.pending_creations[player_id] = PendingCreation(
            player_id=player_id,
            player_name=event.get_sender_name(),
            player_umo=event.unified_msg_origin
        )
        
        yield event.plain_result(
            f"🎮 创建冒险房间\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📝 请输入房间名称（1-30字）\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💡 /tw cancel 取消创建"
        )
    
    @tw.command("quickstart")
    async def cmd_quickstart(self, event: AstrMessageEvent, room_name: str = "快速冒险"):
        """快速创建"""
        player_id = event.get_sender_id()
        
        if self.room_manager.get_room_by_player(player_id):
            yield event.plain_result("❌ 已在房间中")
            return
        
        self.pending_creations.pop(player_id, None)
        
        world = self.world_template or "这是一个充满奇幻与冒险的世界，魔法与剑术并存，危险与机遇共生。"
        
        room = self.room_manager.create_room(
            host_id=player_id,
            host_name=event.get_sender_name(),
            host_umo=event.unified_msg_origin,
            room_name=room_name,
            world_setting=world,
            timeout=self.default_timeout,
            char_timeout=self.char_creation_timeout
        )
        
        if room:
            yield event.plain_result(
                f"⚡ 快速创建成功！\n"
                f"📍 {room.room_name} | 🆔 {room.room_id}\n"
                f"加入: /tw join {room.room_id}\n"
                f"开始: /tw begin"
            )
        else:
            yield event.plain_result("❌ 创建失败")
    
    @tw.command("cancel")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if self.pending_creations.pop(event.get_sender_id(), None):
            yield event.plain_result("✅ 已取消创建")
        else:
            yield event.plain_result("❓ 没有进行中的创建")
    
    @tw.command("join")
    async def cmd_join(self, event: AstrMessageEvent, room_id: str = ""):
        if not room_id:
            yield event.plain_result("❌ 用法: /tw join <房间ID>")
            return
        
        player_id = event.get_sender_id()
        self.pending_creations.pop(player_id, None)
        
        success, msg = self.room_manager.join_room(
            room_id, player_id, event.get_sender_name(),
            event.unified_msg_origin, self.max_players
        )
        
        if success:
            room = self.room_manager.get_room(room_id)
            if room:
                await self._broadcast(room, f"📢 {event.get_sender_name()} 加入！({room.get_active_player_count()}人)")
            yield event.plain_result(f"✅ {msg}")
        else:
            yield event.plain_result(f"❌ {msg}")
    
    @tw.command("begin")
    async def cmd_begin(self, event: AstrMessageEvent):
        """开始游戏"""
        player_id = event.get_sender_id()
        room = self.room_manager.get_room_by_player(player_id)
        
        if not room:
            yield event.plain_result("❌ 你不在任何房间中")
            return
        if not room.is_host(player_id):
            yield event.plain_result("❌ 只有房主可以开始")
            return
        if room.status != RoomStatus.WAITING:
            yield event.plain_result("❌ 游戏已经开始")
            return
        if room.get_active_player_count() < 1:
            yield event.plain_result("❌ 至少需要1名玩家")
            return
        
        room.start_character_creation()
        
        world_preview = room.world_setting[:300] + "..." if len(room.world_setting) > 300 else room.world_setting
        
        await self._broadcast(room, 
            f"🎭 {room.room_name} - 角色创建阶段\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"【世界观预览】\n{world_preview}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏱️ 请在 {room.char_creation_timeout}秒 内完成\n\n"
            f"📝 格式：角色名：背景、性格、技能\n\n"
            f"示例：艾琳：精灵弓箭手，冷静，擅长追踪"
        )
        
        await self._start_char_creation_timeout(room)
        yield event.plain_result("✅ 已开始角色创建阶段")
    
    @tw.command("act")
    async def cmd_act(self, event: AstrMessageEvent, action: str = ""):
        if not action:
            yield event.plain_result("❌ 用法: /tw act <行动描述>")
            return
        
        player_id = event.get_sender_id()
        room = self.room_manager.get_room_by_player(player_id)
        
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        if room.paused:
            yield event.plain_result("❌ 房间已暂停")
            return
        if room.status != RoomStatus.ACTIVE:
            yield event.plain_result("❌ 游戏未开始")
            return
        
        player = room.active_players.get(player_id)
        if not player:
            yield event.plain_result("❌ 非活跃玩家")
            return
        if player.status == PlayerStatus.ACTED:
            yield event.plain_result("❌ 本轮已行动")
            return
        
        player.current_action = action
        player.status = PlayerStatus.ACTED
        player.last_action_time = time.time()
        
        char_name = player.character_name or player.player_name
        yield event.plain_result(f"✅ 【{char_name}】行动已记录")
        
        if room.check_all_players_acted():
            await self._process_round(room)
    
    @tw.command("pause")
    async def cmd_pause(self, event: AstrMessageEvent):
        room = self.room_manager.get_room_by_player(event.get_sender_id())
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        
        success, msg = self.room_manager.pause_room(room.room_id, event.get_sender_id())
        if success:
            await self._stop_timeout(room.room_id)
            await self._broadcast(room, "⏸️ 房间已暂停\n/tw resume 恢复")
            yield event.plain_result("✅ 已暂停")
        else:
            yield event.plain_result(f"❌ {msg}")
    
    @tw.command("resume")
    async def cmd_resume(self, event: AstrMessageEvent):
        room = self.room_manager.get_room_by_player(event.get_sender_id())
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        
        success, msg = self.room_manager.resume_room(room.room_id, event.get_sender_id())
        if success:
            await self._broadcast(room, f"▶️ 继续第{room.current_round}轮")
            await self._start_timeout(room)
            yield event.plain_result("✅ 已恢复")
        else:
            yield event.plain_result(f"❌ {msg}")
    
    @tw.command("status")
    async def cmd_status(self, event: AstrMessageEvent, room_id: str = ""):
        room = self.room_manager.get_room(room_id) if room_id else self.room_manager.get_room_by_player(event.get_sender_id())
        
        if not room:
            yield event.plain_result("❌ 找不到房间")
            return
        
        status_map = {
            RoomStatus.WAITING: "⏳等待中", 
            RoomStatus.CHARACTER_CREATION: "🎭角色创建",
            RoomStatus.ACTIVE: "🎮游戏中", 
            RoomStatus.PAUSED: "⏸️已暂停"
        }
        
        host = room.active_players.get(room.host_id)
        
        info = (
            f"📊 {room.room_name}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🆔 {room.room_id}\n"
            f"👑 {host.player_name if host else '?'}\n"
            f"📊 {status_map.get(room.status, '?')}\n"
            f"🔄 第{room.current_round}轮 | ⏱️{room.timeout}秒\n"
        )
        
        info += f"👥 玩家({room.get_active_player_count()}):\n"
        for p in room.active_players.values():
            char = f"【{p.character_name}】" if p.character_name else ""
            status_icon = {
                PlayerStatus.ACTIVE: "⏳",
                PlayerStatus.ACTED: "✅",
                PlayerStatus.TIMEOUT: "⏰",
                PlayerStatus.CREATING_CHAR: "📝",
                PlayerStatus.CHAR_DONE: "✅"
            }.get(p.status, "?")
            info += f"  {status_icon} {p.player_name} {char}\n"
        
        yield event.plain_result(info)
    
    @tw.command("world")
    async def cmd_world(self, event: AstrMessageEvent):
        room = self.room_manager.get_room_by_player(event.get_sender_id())
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        
        yield self._send_long_message(event, room.world_setting, 
                                       title=f"🌍 世界观 ({len(room.world_setting)}字)")
    
    @tw.command("chars")
    async def cmd_chars(self, event: AstrMessageEvent):
        room = self.room_manager.get_room_by_player(event.get_sender_id())
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        
        chars = room.get_characters_info()
        if chars == "无角色信息":
            yield event.plain_result("❌ 还没有角色信息")
            return
        
        yield self._send_long_message(event, chars, title="👥 角色列表")
    
    @tw.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        rooms = self.room_manager.get_all_rooms()
        
        if not rooms:
            yield event.plain_result("📭 当前没有房间\n/tw start 创建")
            return
        
        lines = ["🏠 房间列表", "━━━━━━━━━━━━━━━━"]
        for r in rooms:
            status = {"waiting": "⏳", "creating": "🎭", "active": "🎮", "paused": "⏸️"}.get(r.status.value, "?")
            lines.append(f"{status} {r.room_name}\n   ID: {r.room_id} | 👥{r.get_active_player_count()}")
        
        yield event.plain_result("\n".join(lines))
    
    @tw.command("close")
    async def cmd_close(self, event: AstrMessageEvent):
        room = self.room_manager.get_room_by_player(event.get_sender_id())
        if not room:
            yield event.plain_result("❌ 不在房间中")
            return
        if not room.is_host(event.get_sender_id()):
            yield event.plain_result("❌ 只有房主可以关闭")
            return
        
        name = room.room_name
        await self._broadcast(room, f"🚫 房间 [{name}] 已关闭")
        await self._stop_timeout(room.room_id)
        await self._stop_timeout(f"char_{room.room_id}")
        self.room_manager.close_room(room.room_id)
        yield event.plain_result(f"✅ 已关闭")
    
    @tw.command("leave")
    async def cmd_leave(self, event: AstrMessageEvent):
        player_id = event.get_sender_id()
        player_name = event.get_sender_name()
        room = self.room_manager.get_room_by_player(player_id)
        
        success, msg = self.room_manager.leave_room(player_id)
        
        if success and room and room.status != RoomStatus.CLOSED:
            await self._broadcast(room, f"📢 {player_name} 离开了房间")
        
        yield event.plain_result(f"{'✅' if success else '❌'} {msg}")
    
    # ==================== 管理员命令 ====================
    
    @tw.command("admin")
    async def cmd_admin(self, event: AstrMessageEvent, action: str = "", target: str = ""):
        """管理员命令"""
        player_id = event.get_sender_id()
        
        if not self._is_admin(player_id):
            yield event.plain_result("❌ 你不是管理员")
            return
        
        if action == "close" and target:
            room = self.room_manager.get_room(target)
            if not room:
                yield event.plain_result(f"❌ 房间 {target} 不存在")
                return
            
            name = room.room_name
            await self._broadcast(room, f"🚫 房间 [{name}] 被管理员强制关闭")
            await self._stop_timeout(room.room_id)
            await self._stop_timeout(f"char_{room.room_id}")
            self.room_manager.close_room(room.room_id)
            yield event.plain_result(f"✅ 已强制关闭 [{name}]")
        
        elif action == "list":
            rooms = self.room_manager.get_all_rooms()
            if not rooms:
                yield event.plain_result("📭 没有房间")
                return
            
            lines = ["🔧 管理员视图", "━━━━━━━━━━━━━━━━"]
            for r in rooms:
                host = r.active_players.get(r.host_id)
                lines.append(
                    f"📍 {r.room_name}\n"
                    f"   ID: {r.room_id}\n"
                    f"   房主: {host.player_name if host else '?'}\n"
                    f"   状态: {r.status.value}\n"
                    f"   玩家: {r.get_active_player_count()}"
                )
            yield event.plain_result("\n".join(lines))
        
        else:
            yield event.plain_result(
                "🔧 管理员命令:\n"
                "/tw admin close <房间ID> - 强制关闭\n"
                "/tw admin list - 详细列表"
            )
    
    @tw.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎮 Textworld 文字冒险\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 创建房间\n"
            "  /tw start - 引导创建\n"
            "  /tw quickstart - 快速创建\n"
            "  /tw cancel - 取消创建\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 加入游戏\n"
            "  /tw join <ID> - 加入房间\n"
            "  /tw leave - 离开房间\n"
            "  /tw list - 房间列表\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎭 游戏命令\n"
            "  /tw begin - 开始游戏\n"
            "  /tw act <行动> - 执行行动\n"
            "  /tw status - 查看状态\n"
            "  /tw world - 查看世界观\n"
            "  /tw chars - 查看角色\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚙️ 房主命令\n"
            "  /tw pause - 暂停\n"
            "  /tw resume - 恢复\n"
            "  /tw close - 关闭房间"
        )
    
    # ==================== AI生成 ====================
    
    async def _generate_opening(self, room: Room) -> str:
        try:
            provider_id = await self.context.get_current_chat_provider_id(room.host_umo)
            if not provider_id:
                return "冒险开始了..."
            
            chars_info = room.get_characters_info()
            
            prompt = f"""你是文字冒险游戏的DM，叙事风格：{self.dm_style}

【世界观】
{room.world_setting[:1500]}

【参与角色】
{chars_info}

请用{self.opening_max_length}字以内生动描述冒险的开场，介绍场景氛围，让每个角色自然地出现在开场场景中。不要替玩家做决定。"""
            
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            return resp.completion_text.strip() if resp and resp.completion_text else "冒险开始了..."
        except Exception as e:
            logger.error(f"生成开场失败: {e}")
            return "冒险开始了..."
    
    async def _process_round(self, room: Room):
        try:
            actions = room.get_round_actions()
            if not actions:
                await self._broadcast(room, "❌ 本轮没有有效行动")
                return
            
            context = room.build_game_context(self.history_rounds)
            action_text = "\n".join([f"- {name}: {act}" for name, act in actions.items()])
            
            provider_id = await self.context.get_current_chat_provider_id(room.host_umo)
            if not provider_id:
                await self._broadcast(room, "❌ 无法获取AI服务")
                return
            
            prompt = f"""你是文字冒险游戏的DM，叙事风格：{self.dm_style}

{context}

【第{room.current_round}轮玩家行动】
{action_text}

请根据玩家行动描述发生的事情和结果，用{self.dm_response_max_length}字以内，保持故事连贯性。不要替玩家做决定。"""
            
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            dm_response = resp.completion_text.strip() if resp and resp.completion_text else "（无响应）"
            
            room.history.append(GameHistory(room.current_round, actions, dm_response))
            room.pending_config.correction_text = None
            
            action_lines = "\n".join([f"  • {name}: {act}" for name, act in actions.items()])
            
            message = (
                f"━━━━ 📖 第{room.current_round}轮结果 ━━━━\n\n"
                f"【玩家行动】\n{action_lines}\n\n"
                f"【DM回应】\n{dm_response}\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🔄 第{room.current_round + 1}轮开始！\n"
                f"⏱️ 超时: {room.timeout}秒\n"
                f"使用 /tw act <行动> 进行冒险"
            )
            
            await self._broadcast(room, message)
            
            room.start_new_round()
            await self._start_timeout(room)
            
        except Exception as e:
            logger.error(f"处理回合失败: {e}")
            await self._broadcast(room, "❌ 处理回合时出错")
    
    # ==================== 超时管理 ====================
    
    async def _start_char_creation_timeout(self, room: Room):
        task_id = f"char_{room.room_id}"
        await self._stop_timeout(task_id)
        
        async def check():
            try:
                await asyncio.sleep(room.char_creation_timeout)
                r = self.room_manager.get_room(room.room_id)
                if not r or r.status != RoomStatus.CHARACTER_CREATION:
                    return
                
                timeout_players = []
                for p in r.active_players.values():
                    if p.status == PlayerStatus.CREATING_CHAR:
                        p.character_name = p.player_name
                        p.character_setting = "一位神秘的冒险者"
                        p.status = PlayerStatus.CHAR_DONE
                        timeout_players.append(p.player_name)
                
                if timeout_players:
                    await self._broadcast(r, f"⏰ 超时: {', '.join(timeout_players)}\n已使用默认角色")
                
                await self._start_game_after_characters(r)
                
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"角色超时失败: {e}")
        
        self.timeout_tasks[task_id] = asyncio.create_task(check())
    
    async def _start_timeout(self, room: Room):
        await self._stop_timeout(room.room_id)
        room_id = room.room_id
        
        async def check():
            try:
                await asyncio.sleep(room.timeout)
                r = self.room_manager.get_room(room_id)
                if not r or r.paused or r.status != RoomStatus.ACTIVE:
                    return
                
                timeout_players = []
                for p in r.active_players.values():
                    if p.status == PlayerStatus.ACTIVE:
                        p.status = PlayerStatus.TIMEOUT
                        timeout_players.append(p.character_name or p.player_name)
                
                if timeout_players:
                    await self._broadcast(r, f"⏰ 超时: {', '.join(timeout_players)}")
                
                if r.check_all_players_timeout():
                    await self._broadcast(r, "🚫 全员超时，房间关闭")
                    self.room_manager.close_room(room_id)
                else:
                    await self._process_round(r)
                    
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"超时失败: {e}")
        
        self.timeout_tasks[room_id] = asyncio.create_task(check())
    
    async def _stop_timeout(self, task_id: str):
        task = self.timeout_tasks.pop(task_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    async def terminate(self):
        for task_id in list(self.timeout_tasks.keys()):
            await self._stop_timeout(task_id)
        self.pending_creations.clear()
        logger.info("Textworld 已卸载")
