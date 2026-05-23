# GitHub 参考项目学习笔记

## 参考项目

### 1. chjw8016/www_job_com (⭐84)
- **平台覆盖**: Boss直聘、智联招聘、51job、赶集、58、拉勾、大街、ChinaHR、内推
- **技术栈**: Scrapy + MySQL
- **最后更新**: 2026-04-24
- **关键文件**:
  - `spiders/zhipin_spider.py` - Boss直聘爬虫
  - `spiders/zhaopin_spider.py` - 智联招聘爬虫
  - `spiders/job51_spider.py` - 前程无忧爬虫

**关键发现**:

1. **Boss直聘 URL 模式**:
   ```
   https://www.zhipin.com/c101180100-p100103/h_101180100/?query=php&page=1&ka=page-1
   ```
   - `c{city_code}` 城市区域编码
   - `p{exp_code}` 经验编码
   - `h_{area_code}` 地区编码
   - `query=` 搜索关键词
   - `page=` 页码
   - `ka=page-{n}` 页面追踪参数

2. **Boss直聘 选择器**:
   - 列表: `div.job-list > ul > li`
   - 职位名: `div.info-primary > h3 > a > div`
   - 薪资: `div.info-primary > h3 > a > span`
   - 城市: `div.info-primary > p::text` (第1个)
   - 经验: `div.info-primary > p::text` (第2个)
   - 学历: `div.info-primary > p::text` (第3个)
   - 公司名: `div.info-company > div.company-text > h3 > a`
   - 行业/融资/规模: `div.info-company > div.company-text > p::text`
   - 发布时间: `div.info-publis > p`
   - 职位ID: `h3 > a::attr(data-jobid)`

3. **前程无忧 URL 模式**:
   ```
   https://search.51job.com/list/170200,000000,0000,00,9,99,php,2,1.html
   ```
   - `170200` 城市编码
   - `php` 搜索关键词
   - `1.html` 页码

4. **前程无忧 选择器**:
   - 列表: `div.dw_table > div.el`
   - 职位名: `span.t1 > span > a` 或 `p.t1 > span > a`
   - 薪资: `span.t4` (需要处理4种格式)
   - 公司: `span.t2 > a`
   - 城市: `span.t3`
   - 职位ID: `p.t1 > input::attr(value)`
   - 时间: `span.t5`
   - 过滤表头: `str_time == "发布时间"`

5. **前程无忧 薪资4种格式**:
   - `万/月`: `1.5万/月-2万/月` → 乘10转K
   - `万/年`: `20万/年-30万/年` → 除12乘10转K/月
   - `千/月`: `8千/月-12千/月` → 直接K
   - `元/天`: 跳过

6. **智联招聘 旧域名**:
   ```
   http://sou.zhaopin.com/jobs/searchresult.ashx?jl=郑州&kw=php&p=1
   ```
   - 表格布局, 选择器: `table.newlist > tr`
   - 偶数行是数据行 (跳过表头)

7. **通用抓取经验**:
   - 间隔: 10秒/页
   - `time.sleep(10)` 在 Scrapy 的 callback 中
   - 延迟非常重要，不可跳过

### 2. Wafaamu3113/boss-cli (逆向API)
- **核心发现**: Boss直聘有完整的内部JSON API
- **API端点**: `/wapi/zpgeek/search/joblist.json`
- **认证**: 浏览器Cookie `__zp_stoken__`
- **反检测策略**:
  - 高斯抖动延迟
  - 禁止并行请求
  - 速率限制自动恢复 (10s→20s→40s→60s)
  - Cookie自动刷新

**关键API参数**:
```
query=关键词
city=城市编码
page=页码
pageSize=30
experience=经验编码
degree=学历编码
salary=薪资编码
```

**API响应字段**:
```json
{
  "zpData": {
    "jobList": [{
      "securityId": "",
      "jobName": "",
      "brandName": "",
      "salaryDesc": "",
      "cityName": "",
      "workingExp": "",
      "degreeName": "",
      "jobDesc": "",
      "industryName": "",
      "companySize": "",
      "welfareList": [],
      "jobTagList": [],
      "activeTimeDesc": ""
    }]
  }
}
```

## 应用到我们的项目改进

1. ✅ Boss直聘选择器更新为多版本兼容 (旧版+新版)
2. ✅ Boss直聘API逆向集成 (/wapi/ 端点)
3. ✅ 前程无忧薪资解析支持4种格式 (万/月, 万/年, 千/月, 元/天)
4. ✅ 智联招聘兼容新旧域名 (sou.zhaopin.com + zhaopin.com)
5. ✅ 高斯抖动延迟替代固定延迟
6. ✅ Cookie认证策略 (__zp_stoken__)
7. ✅ 反检测脚本增强
8. ✅ 速率限制自动恢复策略

## 尚未在参考项目中发现但有价值的补充

- 猎聘 (liepin.com): 无Scrapy参考，但有API拦截模式可用
- Boss直聘新版: boss-cli是最新的逆向实现 (2024-2025)
- 多平台整合: 仅www_job_com做到了全平台
- AI辅助去重: 参考项目都用简单去重，无智能去重
