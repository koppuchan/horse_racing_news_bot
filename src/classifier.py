import re

OIKIRI_KEYWORDS = [
    "追い切り", "調教", "坂路", "ウッド", "Wコース", "併せ馬", "単走", 
    "馬なり", "一杯", "強め", "好時計"
]

RESULT_KEYWORDS = [
    "レース結果", "確定", "着順", "タイム", "払戻", "快勝", "制した", 
    "初制覇", "優勝", "敗退", "レース後コメント", "次走"
]

CAT_OIKIRI = 16
CAT_RESULT = 17
CAT_NEWS = 18

def classify_article(title: str, content: str) -> int:
    """
    Classify article into category ID based on keywords in title and content.
    """
    text = f"{title} {content}"
    
    if any(kw in text for kw in OIKIRI_KEYWORDS):
        return CAT_OIKIRI
        
    if any(kw in text for kw in RESULT_KEYWORDS):
        return CAT_RESULT
        
    return CAT_NEWS
