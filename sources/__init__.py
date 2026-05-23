"""sources package — 公开岗位源聚合层

用法：
    from sources import iter_sources, list_sources_meta
    for s in iter_sources():
        items = await s.fetch(limit=20, keyword="Python", city="北京")
"""
from .base import BaseSource  # noqa: F401
from .sasac import SasacSource
from .mohrss import MohrssSource
from .company import CompanyOfficialSource
from .guoping import GuopingSource
from .huawei import HuaweiSource
from .websearch import WebSearchSource
from .talent_regional import RegionalTalentSource
from .shixiseng import ShixisengSource
from .extra_channels import ExtraChannelSource


# 顺序即 UI 上的展示顺序
# 注：原微信公众号源依赖用户自建 RSSHub + Wechat2RSS（公开实例长期不可用），
#     未配置时只会返回 0，因此从默认注册表中移除。外企岗位通过 WebSearch 兜底
#     —— 用户在前端选择"公司性质=外企"会自动拼到搜索关键词里。
_SOURCE_CLASSES: list[type[BaseSource]] = [
    SasacSource,
    MohrssSource,
    CompanyOfficialSource,
    HuaweiSource,
    GuopingSource,
    RegionalTalentSource,
    ShixisengSource,
    ExtraChannelSource,
    WebSearchSource,
]


def iter_sources() -> list[BaseSource]:
    """返回所有已注册源的实例（一次性，便宜，无副作用）"""
    return [cls() for cls in _SOURCE_CLASSES]


def list_sources_meta() -> list[dict]:
    """供前端渲染源开关 chip 用"""
    return [
        {
            "key": cls.key,
            "name": cls.display_name,
            "category": cls.category,
            "enabled": cls.enabled,
            "bulletin": getattr(cls, "bulletin", False),
        }
        for cls in _SOURCE_CLASSES
    ]
