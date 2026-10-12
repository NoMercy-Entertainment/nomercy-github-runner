import ast
from pathlib import Path
import shutil

import pytest


@pytest.mark.parametrize('existing',[True,False])
def test_module_rollback_restores_old_bytes_or_original_absence(tmp_path,existing):
    script=Path(__file__).resolve().parents[2]/'infra/hyperv/activate-uniform-macos.py'
    tree=ast.parse(script.read_text())
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef)
               and node.name in ('backup_module','restore_modules')]
    root=tmp_path/'backup';root.mkdir()
    live=tmp_path/'agent';(live/'runtimes').mkdir(parents=True)
    relative='runtimes/macos_storage.py'
    target=live/relative
    if existing:target.write_text('original module')
    namespace={'ROOT':root,'LIVE':live,'MODULES':(relative,),'shutil':shutil}
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(script),'exec'),namespace)
    namespace['backup_module'](relative)
    target.write_text('new module')
    namespace['backup_module'](relative)
    namespace['restore_modules']()
    assert target.exists()==existing
    if existing:assert target.read_text()=='original module'
