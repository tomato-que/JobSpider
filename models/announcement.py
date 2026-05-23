"""招聘公告/内推数据模型 — 用于"内推 & 公告" tab

来源:
- 大厂官网公开 API (字节、腾讯等)
- RSSHub 抓取的微信公众号 / 第三方招聘聚合
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Announcement:
    id: str                                 # 稳定 ID: sha1(source + raw_url)[:16]
    source: str                             # 'bytedance' / 'tencent' / 'rsshub:<name>'
    source_type: str                        # 'official' | 'rss'
    title: str
    raw_url: str                            # 原文链接（用户最终点开的地方）

    company: Optional[str] = None
    positions: list[str] = field(default_factory=list)   # 抽取/给定的岗位列表
    apply_url: Optional[str] = None         # 直接投递链接（可能与 raw_url 相同）
    apply_method: Optional[str] = None      # '官网投递' / '邮件' / '内推链接' / '扫码'
    referral_code: Optional[str] = None     # 内推码（如有）
    deadline: Optional[str] = None          # 截止日期 YYYY-MM-DD（如能解析）
    location: Optional[str] = None          # 工作地点（多地用逗号分隔）
    summary: Optional[str] = None           # 80字以内摘要
    education_required: Optional[str] = None   # '本科' / '硕士' / '大专' / '不限' 等
    experience_required: Optional[str] = None  # '3-5年' / '应届' / '不限' 等

    published_at: Optional[str] = None      # 文章/职位发布时间 ISO
    fetched_at: str = ""                    # 入库时间 ISO（写入时填）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")
