#!/usr/bin/env python3
"""Run the actual copy recipe and pinned strict sparse reader on non-A/B maps."""
from pathlib import Path
import hashlib,importlib,json,os,runpy,struct,subprocess,sys,tempfile

def main():
    scripts=Path(__file__).resolve().parent
    h=runpy.run_path(str(scripts/'apply-legacy-ota-map-compat.py'))
    fixture=json.loads((scripts/'ota-image-copy-make.json').read_text())
    macro=fixture['macro'];assert hashlib.sha256(macro.encode()).hexdigest()==fixture['sha256']
    fixed=h['transform'](macro)
    try:h['transform'](macro+macro)
    except ValueError:pass
    else:raise AssertionError('Duplicate recipe accepted')
    with tempfile.TemporaryDirectory(prefix='tb8504-strict-map-') as td:
        base=Path(td);(base/'host/bin').mkdir(parents=True)
        raw=(struct.pack('<I4H4I',0xed26ff3a,1,0,28,12,4096,4,2,0)
             +struct.pack('<2H2I',0xcac1,0,3,12+12288)+b'A'*12288
             +struct.pack('<2H2I',0xcac3,0,1,12))
        image=base/'system.img';image.write_bytes(raw)
        original_map='/system/real-file 1\n/lost+found 2\n<free-space> 3\n'
        generator=base/'host/bin/map_file_generator'
        generator.write_text('#!/bin/sh\nif [ "${3:-}" = --legacy_non_ab ]; then\n printf \'/system/real-file 1\\n<directory:/lost+found> 2\\n\' > "$2"\nelse\n printf \'/system/real-file 1\\n/lost+found 2\\n<free-space> 3\\n\' > "$2"\nfi\n');generator.chmod(0o755)
        readers=json.loads((scripts/'ota-map-sparse-reader-fixture.json').read_text())
        for name,spec in readers.items():
            data=spec['text'].encode();assert hashlib.sha256(data).hexdigest()==spec['sha256']
            if name in ('sparse_img.py','rangelib.py'):
                assert spec['source_head']==h['HEAD'];(base/name).write_bytes(data)
        sys.path.insert(0,str(base));reader=importlib.import_module('sparse_img')
        for ab in ('false','true',''):
            out=base/('pkg-'+(ab or 'unset'));mk=base/'copy.mk'
            mk.write_text('COPY_IMAGES_FOR_TARGET_FILES_ZIP := true\nBOARD_SYSTEMIMAGE_FILE_SYSTEM_TYPE := ext4\nAB_OTA_UPDATER := '+ab+'\nHOST_OUT_EXECUTABLES := '+str(base/'host/bin')+'\nto-upper = $(shell echo $(1) | tr a-z A-Z)\n'+fixed+'all:\n\t$(call copy-image-and-generate-map,'+str(image)+','+str(out)+',system)\n')
            subprocess.run(['make','-s','-f',str(mk)],check=True)
            assert (out/'system.img').read_bytes()==raw
            expected='/system/real-file 1\n<directory:/lost+found> 2\n' if ab=='false' else original_map
            assert (out/'system.map').read_text()==expected
            try:reader.SparseImage(str(out/'system.img'),str(out/'system.map'),'0')
            except AssertionError:
                assert ab!='false'
            else:assert ab=='false'
        map_path=base/'bad.map';map_path.write_text('/system/real-file 3\n')
        try:reader.SparseImage(str(image),str(map_path),'0')
        except AssertionError:pass
        else:raise AssertionError('Invalid allocated file range accepted')
        # Exercise the actual downstream archive-membership and size checks.
        namespace={'os':os,'sparse_img':reader,'RoundUpTo4K':lambda n:((n+4095)//4096)*4096}
        exec(readers['GetSparseImage']['text'],namespace)
        class InputZip:
            def namelist(self):return ['SYSTEM/real-file','ROOT/']
            def getinfo(self,name):
                assert name=='SYSTEM/real-file'
                return type('Info',(),{'file_size':4096})()
        legacy=base/'pkg-false';(legacy/'IMAGES').mkdir()
        (legacy/'IMAGES/system.img').write_bytes(raw)
        (legacy/'IMAGES/system.map').write_text('/system/real-file 1\n/lost+found 2\n')
        try:namespace['GetSparseImage']('system',str(legacy),InputZip(),False)
        except AssertionError as exc:assert 'ZIP entry' in str(exc)
        else:raise AssertionError('Missing lost+found archive regression was not caught')
        (legacy/'IMAGES/system.map').write_text('/system/real-file 1\n<directory:/lost+found> 2\n')
        namespace['GetSparseImage']('system',str(legacy),InputZip(),False)
        # Compile and run the actual C++ filename formatter, including A/B.
        formatter=readers['BlockMapName']['text']
        cpp=base/'formatter.cc';cpp.write_text('#include <string>\n#include <sys/stat.h>\n#include <cassert>\nstruct FilesystemInterface { struct File { std::string name; struct stat file_stat; }; };\n'+formatter+'''int main() {
          FilesystemInterface::File dir{"/lost+found", {}}; dir.file_stat.st_mode=S_IFDIR;
          assert(BlockMapName(dir,true)=="<directory:/lost+found>");
          assert(BlockMapName(dir,false)=="/lost+found");
          FilesystemInterface::File regular{"/system/real-file", {}}; regular.file_stat.st_mode=S_IFREG;
          assert(BlockMapName(regular,true)==regular.name);
          assert(BlockMapName(regular,false)==regular.name);
        }\n''')
        subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(base/'formatter')],check=True)
        subprocess.run([str(base/'formatter')],check=True)
        producer=runpy.run_path(str(scripts/'apply-legacy-map-generator-compat.py'))
        assert producer['digest'](producer['ORIGINAL'].encode())==producer['PRE_FILE_SHA']
        assert producer['digest'](producer['transform'](producer['ORIGINAL']).encode())==producer['POST_FILE_SHA']
        assert formatter in producer['REPLACEMENT']
        # Registration and runtime call are both required for actual delivery.
        autopilot=runpy.run_path(str(scripts/'tb8504-autopilot.py'))
        assert 'apply-legacy-ota-map-compat.py' in autopilot['HELPERS']
        assert 'apply-legacy-map-generator-compat.py' in autopilot['HELPERS']
        assert '"--build-make", str(self.root / "build/make")' in (scripts/'tb8504-autopilot.py').read_text()
        # Fixture Git repo also exercises full-patch rejection and no mutation.
        repo=base/'repo';(repo/'core').mkdir(parents=True);path=repo/'core/Makefile';path.write_text(macro)
        subprocess.run(['git','init','-q',str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','fixture'],check=True)
        apply=h['apply'];g=apply.__globals__
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Unknown immutable HEAD accepted')
        g['HEAD']=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        g['PRE_FILE_SHA']=h['digest'](macro.encode());g['POST_FILE_SHA']=h['digest'](fixed.encode())
        subprocess.run(['git','-C',str(repo),'config','core.abbrev','40'],check=True)
        path.write_text(fixed);g['POST_PATCH_SHA']=h['digest'](subprocess.check_output(['git','-C',str(repo),'diff','--binary','HEAD']));path.write_text(macro)
        assert apply(repo)[0] and not apply(repo)[0]
        path.write_text(fixed+'# unreviewed\n')
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Unknown Makefile accepted')
        path.write_text(macro);(repo/'untracked').write_text('unknown')
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Untracked source accepted')
    print('LEGACY_OTA_MAP_RECIPE_AND_PINNED_STRICT_READER_SELFTEST=PASS')
    print('ALLOCATED_BLOCK_CORRUPTION_STILL_REJECTED=PASS')
    print('AB_RECIPE_UNCHANGED=PASS')
    print('PINNED_GETSPARSEIMAGE_LOST_FOUND_REGRESSION=PASS')
    print('ACTUAL_CPP_DIRECTORY_NAME_FORMATTER=PASS')

if __name__=='__main__':main()
