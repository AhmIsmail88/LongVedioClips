import json
from pathlib import Path
EN=json.loads(Path(__file__).with_name('desktop_translations.json').read_text(encoding='utf-8'))
def translate(value,language):return EN.get(value,value) if language=='en' else value
def translated_strings(language):
    from core.ui_theme import AR_STRINGS
    def convert(value):
        if isinstance(value,dict):return {key:convert(item) for key,item in value.items()}
        if isinstance(value,list):return [convert(item) for item in value]
        return translate(value,language)
    return convert(AR_STRINGS)
