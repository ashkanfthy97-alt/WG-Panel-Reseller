"""Consistent additive commerce backup; no restore or service mutation."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


def backup(instance, output):
    root=(Path(instance)/'telegram-commerce').resolve()
    database=root/'commerce.sqlite3'
    if not database.is_file():raise ValueError('Commerce database not found')
    output=Path(output).resolve()
    if output.is_relative_to(root):raise ValueError('Backup must be outside the commerce data directory')
    descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as archive_file, tempfile.TemporaryDirectory(prefix='commerce-backup-') as directory:
        snapshot=Path(directory)/'commerce.sqlite3'
        with closing(sqlite3.connect(str(database))) as source, closing(sqlite3.connect(str(snapshot))) as destination:
            source.backup(destination)
        with zipfile.ZipFile(archive_file,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot,'telegram-commerce/commerce.sqlite3')
            receipts=root/'receipts'
            if receipts.is_dir():
                for file in sorted(receipts.glob('*.png')):
                    if file.is_file() and not file.is_symlink():archive.write(file,'telegram-commerce/receipts/'+file.name)
            archive.writestr('manifest.json',json.dumps({'format':'wgportal-commerce-backup','version':1,'requires_original_panel_secret':True}))
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Create a private commerce backup alongside the native WG_Panel backup.')
    parser.add_argument('--instance',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(backup(args.instance,args.output))
