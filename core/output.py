"""输出模块 - 格式化结果并保存"""
import csv
import io
import json
import os
from datetime import datetime
from pathlib import Path

from models.job import JobInfo, ScrapeResult
from config import OUTPUT_DIR


def _atomic_write(path: Path, text: str, encoding: str = "utf-8"):
    """先写 .tmp、fsync，再 os.replace，避免中断留下半截文件。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_results_json(results: list[ScrapeResult], all_jobs: list[JobInfo],
                       params: dict, output_dir: Path = OUTPUT_DIR) -> Path:
    """保存结果为JSON"""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_dir / f"jobs_{timestamp}.json"

    output = {
        "meta": {
            "search_params": params,
            "scrape_time": datetime.now().isoformat(),
            "total_jobs": len(all_jobs),
            "platforms_processed": len(results),
        },
        "platform_results": [r.to_dict() for r in results],
        "jobs": [j.to_dict() for j in all_jobs],
    }

    _atomic_write(
        filename,
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return filename


def save_results_csv(all_jobs: list[JobInfo], output_dir: Path = OUTPUT_DIR) -> Path:
    """保存结果为CSV"""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_dir / f"jobs_{timestamp}.csv"

    # 主要字段
    fieldnames = [
        "job_title", "company_name", "salary_range", "salary_min", "salary_max",
        "city", "district", "experience_required", "education_required",
        "platform_name", "job_url", "publish_time", "company_industry",
        "company_size", "benefits", "apply_status", "greeting_sent",
        "apply_skip_reason", "scrape_time",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for job in all_jobs:
        row = job.to_dict()
        row["benefits"] = "|".join(job.benefits)
        writer.writerow(row)
    _atomic_write(filename, buf.getvalue(), encoding="utf-8-sig")

    return filename


def format_summary(results: list[ScrapeResult], all_jobs: list[JobInfo],
                   params: dict) -> str:
    """生成可读的文本摘要"""
    lines = []
    lines.append("=" * 60)
    lines.append("  招聘信息抓取报告")
    lines.append("=" * 60)
    lines.append(f"  搜索条件: {params.get('keyword', 'N/A')} | "
                  f"{params.get('city', '不限')}")
    if params.get("salary_min") or params.get("salary_max"):
        lines.append(f"  薪资范围: {params.get('salary_min', 0)}K - "
                      f"{params.get('salary_max', '不限')}K")
    lines.append(f"  抓取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("-" * 60)

    # 各平台统计
    total_before = 0
    for r in results:
        total_before += r.total_count
        status_icon = {
            "success": "[OK]",
            "partial": "[~]",
            "failed": "[X]",
            "skipped": "[-]",
        }.get(r.status.value, "[?]")
        lines.append(f"  {status_icon} {r.platform_name:8s} : "
                      f"{r.total_count:4d} 条 | {r.status.value}")

    lines.append("-" * 60)
    lines.append(f"  总计: {total_before} 条 (去重后: {len(all_jobs)} 条)")

    # 薪资分析
    salaries = [j.avg_salary for j in all_jobs if j.avg_salary]
    if salaries:
        lines.append(f"  薪资范围: {min(salaries):.0f}K - "
                      f"{max(salaries):.0f}K | "
                      f"中位数: {sorted(salaries)[len(salaries)//2]:.0f}K")

    # 城市分布 Top5
    city_count = {}
    for j in all_jobs:
        if j.city:
            city_count[j.city] = city_count.get(j.city, 0) + 1
    if city_count:
        top_cities = sorted(city_count.items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append(f"  热门城市: {', '.join(f'{c}({n})' for c, n in top_cities)}")

    lines.append("=" * 60)
    return "\n".join(lines)
