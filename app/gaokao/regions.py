"""生源地（哪个省的位次）和院校所在地（筛选范围）是两个独立维度。"""
CANDIDATE_REGIONS = ("四川", "广东", "上海")
SCHOOL_REGIONS = tuple("北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆 香港 澳门 台湾".split())
_REGION_NAMES = {"内蒙古自治区":"内蒙古","广西壮族自治区":"广西","西藏自治区":"西藏","宁夏回族自治区":"宁夏",
                 "新疆维吾尔自治区":"新疆","香港特别行政区":"香港","澳门特别行政区":"澳门"}


def short_region(value):
    value=value.strip()
    return _REGION_NAMES.get(value, value.removesuffix("省").removesuffix("市"))


def candidate_region(value):
    region=short_region(value)
    if region not in CANDIDATE_REGIONS:
        raise ValueError("目前仅支持四川、广东、上海考生，以考生所在省份的投档数据查询。")
    return region


def target_regions(value):
    if value is None: return []
    if isinstance(value,str): value=[value]
    if not isinstance(value,(tuple,list)) or any(not isinstance(v,str) or not v.strip() for v in value):
        raise ValueError("院校目标地区应为省级地区；不限地区请留空。")
    result=[short_region(v) for v in value]
    if any(v not in SCHOOL_REGIONS for v in result):
        raise ValueError("院校目标地区应为省级地区；不限地区请留空。")
    return list(dict.fromkeys(result))
