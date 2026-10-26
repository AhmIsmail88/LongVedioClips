"""Streaming atomic ZIP export of already completed clips."""
from pathlib import Path
import os,tempfile,zipfile

def create_archive(paths,target):
    paths=[Path(p).resolve() for p in paths];target=Path(target).resolve()
    if not paths:raise ValueError('لا توجد مقاطع جاهزة')
    if target in paths:raise ValueError('اختر مسارًا مختلفًا عن ملفات المقاطع')
    for path in paths:
        if not path.is_file():raise ValueError('ملف مقطع غير موجود: '+path.name)
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=target.stem+'-',suffix='.partial',dir=target.parent);os.close(fd)
    try:
        used=set()
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as bundle:
            for index,path in enumerate(paths,1):
                name=path.name
                if name in used:name=f'{index}_{name}'
                used.add(name);bundle.write(path,name)
        os.replace(temp,target)
    finally:Path(temp).unlink(missing_ok=True)
    return str(target)
