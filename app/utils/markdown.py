"""Markdown 渲染为安全的 HTML（消毒后可直接给前端 v-html 展示）。"""
import markdown
import nh3

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

# 允许的标签/属性白名单，防止 LLM 输出注入脚本
_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "blockquote",
    "ul", "ol", "li", "pre", "code", "table", "thead", "tbody",
    "tr", "th", "td", "a", "span", "div", "img",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "img": {"src", "alt", "title"},
}


def render_markdown(text: str) -> str:
    """Markdown -> 安全 HTML。"""
    html = markdown.markdown(text, extensions=_MD_EXTENSIONS, output_format="html")
    html = nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES)
    # 图片懒加载（loading 属性不在消毒白名单里，清洗后再加）
    return html.replace("<img ", '<img loading="lazy" ')
