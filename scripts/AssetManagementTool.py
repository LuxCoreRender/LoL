import bpy
import sys
import json
import hashlib
import zipfile
import zlib
import tempfile

from os import listdir, chdir, remove
from os.path import isfile, join, basename, dirname, splitext, exists
from shutil import copyfile

from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, StringProperty, CollectionProperty, PointerProperty

from mathutils import Vector, Matrix
from ftplib import FTP_TLS
from io import BytesIO, StringIO

import subprocess
import threading

# Icons    
EXPANDABLE_CLOSED = "TRIA_RIGHT"
EXPANDABLE_OPENED = "TRIA_DOWN"
IMAGE = "IMAGE_DATA"
INFO = "INFO"
WARNING = "ERROR"
ERROR = "CANCEL"

ADD = "ADD"  # + sign
REMOVE = "REMOVE"  # - sign, used to remove one element from a collection
CLEAR = "X"  # x sign, used to clear a link (e.g. the world volume)
    

def calc_bbox(objects):
    bbox_min = [10000, 10000, 10000]
    bbox_max = [-10000, -10000, -10000]

    deps = bpy.context.evaluated_depsgraph_get()
    
    for obj in objects:
        obj = obj.evaluated_get(deps)
        
        bbox_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]

        for corner in bbox_corners:
            bbox_min[0] = min(bbox_min[0], corner[0])
            bbox_min[1] = min(bbox_min[1], corner[1])
            bbox_min[2] = min(bbox_min[2], corner[2])

            bbox_max[0] = max(bbox_max[0], corner[0])
            bbox_max[1] = max(bbox_max[1], corner[1])
            bbox_max[2] = max(bbox_max[2], corner[2])

    return (bbox_min, bbox_max)


def calc_hash(filename):
    BLOCK_SIZE = 65536 
    file_hash = hashlib.sha256() 
    with open(filename, 'rb') as file: 
        block = file.read(BLOCK_SIZE)
        while len(block) > 0:
            file_hash.update(block)
            block = file.read(BLOCK_SIZE)
    return file_hash.hexdigest()  


def settings_toggle_icon(enabled):
    return EXPANDABLE_OPENED if enabled else EXPANDABLE_CLOSED


def switch_assettype(self, context): 
    ui_props = context.scene.editAsset
    ui_props.assets.clear()
    ui_props.new_assets.clear()
    
    if ui_props.asset_type == "MATERIAL":
        ui_props.blendermarket_assets = False
    
    bpy.ops.scene.luxcore_ol_load_toc_from_git_repository()


def switch_assetsorttype(self, context):
    ui_props = context.scene.editAsset


def switch_blendermarket(self, context): 
    ui_props = context.scene.editAsset
    if ui_props.blendermarket_assets:
        ui_props.asset_type = "MODEL"
        
    ui_props.assets.clear()
    ui_props.new_assets.clear()
    bpy.ops.scene.luxcore_ol_load_toc_from_git_repository()


def update_filepath(self, context):
    ui_props = context.scene.editAsset
    ui_props.new_assets.clear()


def update_repopath(self, context):
    ui_props = context.scene.editAsset
    
    if exists(join(ui_props.repopath,'.git')):
        ui_props.git_repo = True
        ui_props.progress_info = 'Updating repository'
        
        chdir(ui_props.repopath)
        process = subprocess.Popen('git-lfs fetch', stdout=subprocess.PIPE)
        process.wait()
        
        process = subprocess.Popen('git-lfs checkout', stdout=subprocess.PIPE)
        process.wait()
        ui_props.progress_info = ''
        chdir('..')
    
        bpy.ops.scene.luxcore_ol_load_toc_from_git_repository()
    

def load_assets(filepath):
    new_assets = []
    
    for f in [file for file in listdir(filepath) if isfile(join(filepath, file)) and splitext(file)[1] == '.blend']:
        asset = {}
        asset['name'] = splitext(f)[0].replace('_',' ') 
        asset['url'] = splitext(f)[0]+'.zip'
        asset['category'] = 'Misc'

        filename = asset['url']
        blendfile = join(filepath, splitext(filename)[0] + '.blend')


        with bpy.data.libraries.load(blendfile, link=True) as (data_from, data_to):
            data_to.objects = [name for name in data_from.objects]
        
        if ui_props.asset_type == 'MODEL':
            (bbox_min, bbox_max) = calc_bbox(data_to.objects)
            asset['bbox_min'] = bbox_min
            asset['bbox_max'] = bbox_max
        asset['hash'] = calc_hash(blendfile)
        
        tpath = join(filepath, splitext(filename)[0] + '.jpg')

        img = None
        if exists(tpath):
            img = bpy.data.images.load(tpath)
            img.name = '.LOL_preview'

        asset['thumbnail'] = img
            
        new_assets.append(asset)
                        
        bpy.ops.object.delete() 

        leftOverObjBlocks = [block for block in bpy.data.objects if block.users == 0]
        for block in leftOverObjBlocks:
            bpy.data.objects.remove(block)

        leftOverMeshBlocks = [block for block in bpy.data.meshes if block.users == 0]
        for block in leftOverMeshBlocks:
            bpy.data.meshes.remove(block)
    
    return new_assets


class LuxCoreOnlineLibraryAsset(bpy.types.PropertyGroup):
    name: StringProperty(name='Asset name', description='Assign a name to the asset', default='Default')
    category: StringProperty(name='Category', description='Assign a category to the asset', default='misc')
    url: StringProperty(name='Url', description='Assign a category to the asset', default='')
    bbox_min: FloatVectorProperty(name='Bounding Box Min', default=(0, 0, 0))
    bbox_max: FloatVectorProperty(name='Bounding Box Max', default=(1, 1, 1))
    hash: StringProperty(name='Hash', description='SHA256 hash number for the asset blendfile', default='')
    show_settings: BoolProperty(default=False)
    show_thumbnail: BoolProperty(name='', default=True, description='Show thumbnail')
    new: BoolProperty(name='', default=False, description='New Asset')
    deleted: BoolProperty(name='', default=False, description='Deleted Asset')
    thumbnail: PointerProperty(name='Image', type=bpy.types.Image)


class LOLCheckPathOperator(Operator):
    bl_idname = 'scene.luxcore_ol_check_path'
    bl_label = 'LuxCore Online Library Check Path'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    filepath: StringProperty(name='filepath', default='', options={'SKIP_SAVE'})
      
    @classmethod
    def description(cls, context, properties):
        return 'Check path for new assets'


    def execute(self, context):
        ui_props = context.scene.editAsset
        ui_props.messages.clear()
        
        new_assets_prop = ui_props.new_assets
        new_assets_prop.clear()
        new_assets = load_assets(self.filepath)
        namelist = [asset['name'] for asset in ui_props.assets]
        
        sorted_assets = sorted(new_assets, key=lambda c: c['name'].lower())
        
        for asset in sorted_assets:
            if asset['name'] in namelist:
                print('Found in Assets:', asset['name'])
                
            new_asset = new_assets_prop.add()
            new_asset['name'] = asset['name']
            new_asset['url'] = asset['url']
            new_asset['category'] = 'Misc'
            new_asset['hash'] =  asset['hash']
            if ui_props.asset_type == 'MODEL':
                new_asset['bbox_min'] = asset['bbox_min']
                new_asset['bbox_max'] = asset['bbox_max']
            new_asset['thumbnail'] = asset['thumbnail']
                  
        return {'FINISHED'}


class LOLClearMessagesOperator(Operator):
    bl_idname = 'scene.luxcore_ol_clear_messages'
    bl_label = 'LuxCore Online Library Clear Messages'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Clear messages'

    def execute(self, context):
        context.scene.editAsset.messages.clear()
    
        return {'FINISHED'}

    
class LOLRemoveAssetOperator(Operator):
    bl_idname = 'scene.luxcore_ol_remove_asset'
    bl_label = 'LuxCore Online Library Remove Asset'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    asset_index: IntProperty(name='asset_index', default=-1, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return 'Remove asset from the database'

    def execute(self, context):
        ui_props = context.scene.editAsset
        sorted_assets = sorted(ui_props.assets, key=lambda c: c.name.lower())
        asset = sorted_assets[self.asset_index] 
        asset.deleted = True
    
        return {'FINISHED'}
 

class LOLAddAssetOperator(Operator):
    bl_idname = 'scene.luxcore_ol_add_asset'
    bl_label = 'LuxCore Online Library Add Asset'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    asset_index: IntProperty(name='asset_index', default=-1, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return 'Add asset to the database'


    def execute(self, context):
        ui_props = context.scene.editAsset
    
        asset = ui_props.new_assets[self.asset_index]
        namelist = [asset['name'] for asset in ui_props.assets if not asset.deleted]
        hashlist = [asset['hash'] for asset in ui_props.assets if not asset.deleted]
        
        ui_props.messages.clear()

        if asset['hash'] in hashlist:
            ui_props.messages.append(asset['name'] +': Asset with same hash number is already in database. Asset not added.')
            print(ui_props.messages)
            print('Info ' + asset['name'] +': Asset with same hash number is already in database. Asset not added.')
        elif asset['name'] in hashlist:
            ui_props.messages.append(asset['name'] +': Asset with same name is already in database. Asset not added.')
            print('Info ' + asset['name'] +': Asset with same name is already in database. Asset not added.')
        else:
            print('Added ' + asset['name'] + ' to database')
            
            asset_prop = ui_props.assets.add()
            asset_prop['name'] = asset['name']
            asset_prop['url'] = asset['url']
            asset_prop['category'] = asset['category']
            if ui_props.asset_type == 'MODEL':
                asset_prop['bbox_min'] = asset['bbox_min']
                asset_prop['bbox_max'] = asset['bbox_max']
            asset_prop['hash'] = asset['hash']
            asset_prop['thumbnail'] = asset['thumbnail']
            asset_prop['new'] = True
           
            ui_props.new_assets.remove(self.asset_index)
            
        return {'FINISHED'}

 
class LOLAddAllAssetOperator(Operator):
    bl_idname = 'scene.luxcore_ol_add_all_asset'
    bl_label = 'LuxCore Online Library Add All Assets'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Add all new assets to the database'


    def execute(self, context):
        ui_props = context.scene.editAsset
    
        namelist = [asset['name'] for asset in ui_props.assets if not asset.deleted]
        hashlist = [asset['hash'] for asset in ui_props.assets if not asset.deleted]
        
        ui_props.messages.clear()
        
        for asset in ui_props.new_assets:
            add_asset = True
            if asset['hash'] in hashlist:
                ui_props.messages.append(asset['name'] +': Asset with same hash number is already in database. Asset not added.')
                print('Info ' + asset['name'] +': Asset with same hash number is already in database. Asset not added.')
                add_asset = False
            elif asset['name'] in namelist:
                ui_props.messages.append(asset['name'] +': Asset with same name is already in database. Update asset.')
                print('Info ' + asset['name'] +': Asset with same name is already in database. Update asset.')
            
            if add_asset:
                print('Added ' + asset['name'] + ' to database')
                
                asset_prop = ui_props.assets.add()
                asset_prop['name'] = asset['name']
                asset_prop['url'] = asset['url']
                asset_prop['category'] = asset['category']
                if ui_props.asset_type == 'MODEL':
                    asset_prop['bbox_min'] = asset['bbox_min']
                    asset_prop['bbox_max'] = asset['bbox_max']
                asset_prop['hash'] = asset['hash']
                asset_prop['thumbnail'] = asset['thumbnail']
                asset_prop['new'] = True
                   
        ui_props.new_assets.clear()
            
        return {'FINISHED'}


class LOLLoadTOCfromGitRepositoy(Operator):
    bl_idname = 'scene.luxcore_ol_load_toc_from_git_repository'
    bl_label = 'LuxCore Online Library Load TOC from GIT Repository'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Load TOC from Git Repository'
    
    def cloneRepository(self, context):
        import subprocess
        ui_props = context.scene.editAsset 
        process = subprocess.Popen("git clone https://github.com/LuxCoreRender/LoL " + ui_props.repopath, stdout=subprocess.PIPE)

                
    def execute(self, context):
        ui_props = context.scene.editAsset 
        assets = ui_props.assets
        user_preferences = context.preferences.addons['BlendLuxCore'].preferences

        edit_assets_prop = ui_props.assets
        edit_assets_prop.clear()
        
        if ui_props.blendermarket_assets:
            filename = 'assets_model_patreon.json'
        elif ui_props.asset_type == 'MATERIAL':
            filename = 'assets_material.json'
        else: 
            filename = 'assets_model.json'
        
        filepath = join(ui_props.repopath, filename)
        if isfile(filepath):
            with open(filepath) as file_handle:
                assets = json.loads(file_handle.read())
        
        #TODO: Sort assets by name
        for asset in assets:
            new_asset = edit_assets_prop.add()
            new_asset['name'] = asset['name']
            new_asset['url'] = asset['url']
            new_asset['category'] = asset['category']
            new_asset['hash'] =  asset['hash']
            if ui_props.asset_type == 'MODEL':
                new_asset['bbox_min'] = asset['bbox_min']
                new_asset['bbox_max'] = asset['bbox_max']
            
            assetpath = join(user_preferences.global_dir, ui_props.asset_type.lower())
            thumbnailname = splitext(new_asset['url'])[0] + '.jpg'
               
            tpath = join(assetpath, 'preview', thumbnailname)
            if exists(tpath):
                img = bpy.data.images.load(tpath)
                img.name = '.LOL_preview'
                asset['thumbnail'] = img
                new_asset['thumbnail'] = asset['thumbnail']              

        return {'FINISHED'}

    
class LOLUploadTOCOperator(Operator):
    bl_idname = 'scene.luxcore_ol_upload_toc'
    bl_label = 'LuxCore Online Library Upload ToC'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Upload table of context to the server'

    def connect(self, context):
        ui_props = context.scene.editAsset

        ftp = FTP_TLS()
        ftp.connect('ftp.luxcorerender.org', 21)
        ftp.login(ui_props.username, ui_props.password)
        return ftp

    def uploadToC(self, context, assets):
        ui_props = context.scene.editAsset
        
        ftp = self.connect(context)
        bytestream2 = BytesIO()
        bytestream2.write(json.dumps(assets, indent=2).encode('utf-8'))
        bytestream2.seek(0)
        
        if ui_props.blendermarket_assets:
            filename = 'assets_model_patreon.json'
        elif ui_props.asset_type == 'MATERIAL':
            filename = 'assets_material.json'
        else: 
            filename = 'assets_model.json'
            
        ftp.storbinary(f'STOR {filename}', bytestream2)
        ftp.quit()
        
        
    def uploadFiles(self, context, assets):
        ui_props = context.scene.editAsset
        
        if ui_props.asset_type == 'MATERIAL':
            ftppath = '/material'
        else: 
            ftppath = '/model'
        
        ftp = self.connect(context)

        with tempfile.TemporaryDirectory() as temp_dir_path:   
            for asset in [asset for asset in assets if 'new' in asset.keys()]:
                if not ui_props.blendermarket_assets:
                    temp_zip_path = join(temp_dir_path, asset['url'])
                 
                    ftp.cwd(ftppath)      
                    chdir(ui_props.filepath)
                    with zipfile.ZipFile(temp_zip_path, mode='w') as zf:
                        zf.write(splitext(asset['url'])[0]+'.blend', compress_type=zipfile.ZIP_DEFLATED)
            
                    with open(temp_zip_path,'rb') as file:
                        filename = asset['url']
                        ftp.storbinary(f'STOR {filename}', file)
                
                ftp.cwd(ftppath+'/preview')
                with open(asset['thumbnail'].filepath,'rb') as file:
                    filename = splitext(asset['url'])[0]+'.jpg'
                    ftp.storbinary(f'STOR {filename}', file)      
        
        used_images = [splitext(a.url)[0]+'.jpg' for a in assets if not a.deleted]
        # Delete files which are not needed anymore
        for asset in [asset for asset in assets if asset.deleted]:
            if not ui_props.blendermarket_assets:
                ftp.cwd(ftppath)
                filename = asset['url']
                ftp.delete(filename)

            ftp.cwd(ftppath+'/preview')
            with open(asset['thumbnail'].filepath,'rb') as file:
                filename = splitext(asset['url'])[0]+'.jpg'
                if not filename in used_images: 
                    ftp.delete(filename)

        ftp.quit()
    
           
    def execute(self, context):
        ui_props = context.scene.editAsset
    
        assets = []
        for asset in ui_props.assets:
            if not asset.deleted:
                new_asset = {}
                new_asset['name'] = asset['name']
                new_asset['url'] = asset['url']
                new_asset['category'] = asset['category']
                new_asset['hash'] =  asset['hash']
     
                if ui_props.asset_type == 'MODEL':
                    new_asset['bbox_min'] = [asset['bbox_min'][0],asset['bbox_min'][1],asset['bbox_min'][2]]
                    new_asset['bbox_max'] = [asset['bbox_max'][0],asset['bbox_max'][1],asset['bbox_max'][2]]
            
                assets.append(new_asset)    
          
        self.uploadToC(context, assets)
        self.uploadFiles(context, ui_props.assets)     
              
        return {'FINISHED'}
    

class LOLUpdateGitRepositoy(Operator):
    bl_idname = 'scene.luxcore_ol_update_git_repository'
    bl_label = 'LuxCore Online Library Update GIT Repository'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Update Git Repository with changed assets'
    
    def saveToC(self, context, assets):
        ui_props = context.scene.editAsset
        
        if ui_props.blendermarket_assets:
            filename = 'assets_model_patreon.json'
        elif ui_props.asset_type == 'MATERIAL':
            filename = 'assets_material.json'
        else: 
            filename = 'assets_model.json'

        assets = []
        for asset in ui_props.assets:
            if not asset.deleted:
                new_asset = {}
                new_asset['name'] = asset['name']
                new_asset['url'] = asset['url']
                new_asset['category'] = asset['category']
                new_asset['hash'] =  asset['hash']
     
                if ui_props.asset_type == 'MODEL':
                    new_asset['bbox_min'] = [asset['bbox_min'][0],asset['bbox_min'][1],asset['bbox_min'][2]]
                    new_asset['bbox_max'] = [asset['bbox_max'][0],asset['bbox_max'][1],asset['bbox_max'][2]]
            
                assets.append(new_asset)
        
        with open(join(ui_props.repopath, filename),'w') as file:   
            file.write(json.dumps(assets, indent=2))
                
    def execute(self, context):
        ui_props = context.scene.editAsset 
        assets = ui_props.assets
                    
        #Copy files    
        if ui_props.asset_type == 'MATERIAL':
            typepath = 'material'
        else: 
            typepath = 'model'

        with tempfile.TemporaryDirectory() as temp_dir_path:   
            for asset in [asset for asset in assets if 'new' in asset.keys()]:
                if not ui_props.blendermarket_assets:
                    temp_zip_path = join(temp_dir_path, asset['url'])
                     
                    chdir(ui_props.filepath)
                    # compress .blend file as zip
                    with zipfile.ZipFile(temp_zip_path, mode='w') as zf:
                        zf.write(splitext(asset['url'])[0]+'.blend', compress_type=zipfile.ZIP_DEFLATED)
            
                    print('Copy file:', temp_zip_path)
                    copyfile(temp_zip_path, join(ui_props.repopath, typepath, asset['url']))
                
                print('Copy Image:', asset['thumbnail'].filepath)                    
                copyfile(asset['thumbnail'].filepath, join(ui_props.repopath, typepath, 'preview', splitext(asset['url'])[0]+'.jpg'))

        
        used_images = [splitext(a.url)[0]+'.jpg' for a in assets if not a.deleted]
        # Delete files which are not needed anymore
        for asset in [asset for asset in assets if asset.deleted]:
            if not ui_props.blendermarket_assets:
                filename = join(ui_props.repopath, typepath, asset['url'])
                if exists(filename):
                    remove(filename) 

            filename = join(ui_props.repopath, typepath, 'preview', splitext(asset['url'])[0]+'.jpg')
            if exists(filename) and not splitext(asset['url'])[0]+'.jpg' in used_images:
                remove(filename)
        
        self.saveToC(context, assets)
        
        import subprocess
        chdir(ui_props.repopath)
        
        if ui_props.blendermarket_assets:
            filename = 'assets_model_patreon.json'
        elif ui_props.asset_type == 'MATERIAL':
            filename = 'assets_material.json'
        else: 
            filename = 'assets_model.json'
        
        #Add table of contents file    
        process = subprocess.Popen("git add " + filename, stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()

        #Add asset files to commit
        process = subprocess.Popen("git add " + typepath, stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()

        process = subprocess.Popen("git status", stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()
         
        #Commit changes
        process = subprocess.Popen('git commit -a -m "Update Assets"', stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()
        
        #Pull commits from server
        process = subprocess.Popen("git pull", stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()

        #Push commits to server
        process = subprocess.Popen("git push", stdout=subprocess.PIPE)
        print(process.communicate()[0].decode('utf-8'))
        process.wait()
        
        # Update Server
        bpy.ops.scene.luxcore_ol_upload_toc()
        
        return {'FINISHED'}


class BackgroundThread(threading.Thread):
    def __init__(self, context):
        self.context = context
        self._stop_event = threading.Event()
        threading.Thread.__init__(self)
        
    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        from time import sleep
        ui_props = self.context.scene.editAsset
        
        ui_props.progress_info = 'Cloning git repository...'
        print('Cloning git repository...')
        self.context.window_manager.windows.update()
        process = subprocess.Popen('git clone https://github.com/LuxCoreRender/LoL ' + ui_props.repopath, stdout=subprocess.PIPE)
        process.wait()
        
        ui_props.progress_info = 'Fetching LFS objects...'
        print('Fetching LFS objects...')
        
        chdir(ui_props.repopath)
        process = subprocess.Popen('git-lfs fetch', stdout=subprocess.PIPE)
        process.wait()
        
        ui_props.progress_info = 'Checkout LFS objects...'
        print('Checkout LFS objects...')
        
        process = subprocess.Popen('git-lfs checkout', stdout=subprocess.PIPE)
        process.wait()
        
        ui_props.git_repo = True
        chdir('..')
        print('finished')



class LOLCloneGitRepositoy(Operator):
    bl_idname = 'scene.luxcore_ol_clone_git_repository'
    bl_label = 'LuxCore Online Library Clone GIT Repository'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        return 'Clone Git Repository'
    
    def execute(self, context):
        ui_props = context.scene.editAsset
        wm = bpy.context.window_manager
        
        if not ui_props.gitclone:
            ui_props.gitclone = True
            thread = BackgroundThread(context)
            wm.progress_begin(0, 100)
            wm.progress_update(1)
        
            thread.start()
            return {'PASS_THROUGH'}
        else:
            if ui_props.git_repo:
                ui_props.gitclone = False
                bpy.ops.scene.luxcore_ol_load_toc_from_git_repository()
                wm.progress_end()
                return {'FINISHED'}
            else:
                wm.progress_begin(0, 100)
                if ui_props.progress_info == 'Cloning git repository...':
                    wm.progress_update(1)
                elif ui_props.progress_info == 'Fetching LFS objects...':
                    wm.progress_update(33)
                elif ui_props.progress_info == 'Checkout LFS objects...':
                    wm.progress_update(66)
                    
            return {'PASS_THROUGH'}


class VIEW3D_PT_LUXCORE_ONLINE_LIBRARY_EDIT_ASSETS(Panel):
    bl_category = 'LuxCoreOnlineLibrary'
    bl_idname = 'VIEW3D_PT_LUXCORE_ONLINE_LIBRARY_EDIT_ASSETS'
    bl_space_type = 'TEXT_EDITOR'
    bl_region_type = 'UI'
    bl_label = 'Edit Assets'
    bl_order = 0
    
    
    def draw_login_info(self, context, layout):
        ui_props = context.scene.editAsset
        col = layout.column(align=True)       
        col.label(text='Server Login:')        
        col.prop(ui_props, 'username')
        col = layout.column(align=True)       
        col.prop(ui_props, 'password')
        layout.separator()

    
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        ui_props = context.scene.editAsset

        col = layout.column(align=True)
        col.prop(ui_props, 'repopath')    
                
        if not ui_props.git_repo:
            col = layout.column(align=True)
            if not ui_props.gitclone:
                op = col.operator('scene.luxcore_ol_clone_git_repository', text='Clone git repository')
            if not ui_props.progress_info == '':
                col = layout.column(align=True) 
                col.label(text=ui_props.progress_info, icon=INFO)            
        else:
            col = layout.column(align=True)
        
            if ui_props.username == '' or ui_props.password =='':
                col.enabled = False
            else:
                col.enabled = True
            
            op = col.operator('scene.luxcore_ol_update_git_repository', text='Update Git Repository')
    
            self.draw_login_info(context, layout)

            row = layout.row()
            row.scale_x = 1.6
            row.scale_y = 1.6
            row.use_property_split = False
            row.prop(ui_props, 'asset_type', expand=True, icon_only=True)

            col = row.column(align=True) 
            col.prop(ui_props, 'blendermarket_assets', text='Blendermarket Assets')
            col = layout.column(align=True)
            col.prop(ui_props, 'advanced_settings', text='Advanced Settings')
            
            layout.separator()
            row = layout.row(align=True)
            col = row.column(align=True)
            col.label(text='Assets:')
            
            col = row.column(align=True)
            col.prop(ui_props, "asset_sorttype", text="Sort by:", expand=False, icon_only=False)

            
            col = layout.column(align=True)
            box = col.box()
            row = box.row()
            row.use_property_split = False
            col = row.column()
            
            col.prop(ui_props, 'show_assets',
                     icon=settings_toggle_icon(ui_props.show_assets),
                     icon_only=True, emboss=False)
            col = row.column()
            
            col.label(text='{0} assets found'.format(len([asset for asset in ui_props.assets if not asset.deleted])))      

            if ui_props.show_assets:
                if ui_props.asset_sorttype == 'NAME':
                    sorted_assets = sorted(ui_props.assets, key=lambda c: c.name.lower())
                if ui_props.asset_sorttype == 'CATEGORY':
                    sorted_assets = sorted(ui_props.assets, key=lambda c: c.name.lower())
                    sorted_assets = sorted(sorted_assets, key=lambda c: c.category.lower())
                      
                for idx, asset in enumerate(sorted_assets):
                    if not asset.deleted:
                        self.draw_assetlist(box, asset, idx, True)
          
            layout.separator()
            col = layout.column(align=True)       
            col.label(text='New Assets:')        
            col = layout.column(align=True)       
            col.prop(ui_props, 'filepath')
            col = layout.column(align=True)
            
            op = col.operator('scene.luxcore_ol_check_path', text='Check path for assets')
            op.filepath = ui_props.filepath
            
            col = layout.column(align=True)    
            
            if len(ui_props.messages):
                op = col.operator('scene.luxcore_ol_clear_messages', text='Clear Messages')

            for message in ui_props.messages:
                col = layout.column(align=True) 
                col.label(text=message, icon=WARNING)
                
            if len(ui_props.new_assets):
                col = layout.column(align=True)       
                op = col.operator('scene.luxcore_ol_add_all_asset', text='Add all assets')

                col = layout.column(align=True)       
                box = col.box()
                row = box.row()
                col = row.column()
                col.prop(ui_props, 'show_new_assets',
                         icon=settings_toggle_icon(ui_props.show_new_assets),
                         icon_only=True, emboss=False)
                col = row.column()
                col.label(text='{0} assets found'.format(len(ui_props.new_assets)))       

                if ui_props.show_new_assets:
                    sorted_assets = sorted(ui_props.new_assets, key=lambda c: c.name.lower())        
                    for idx, asset in enumerate(ui_props.new_assets):
                        self.draw_assetlist(box, asset, idx)


    def draw_assetlist(self, layout, asset, idx, add_remove=False):
        col = layout.column(align=True)
        # Upper row (enable/disable, name, remove)
        box = col.box()
        row = box.row()
        row.use_property_split = False
        col = row.column()
        col.prop(asset, 'show_settings',
                 icon=settings_toggle_icon(asset.show_settings),
                 icon_only=True, emboss=False)
        
        col = row.column()
        if asset.new:  
            col.prop(asset, 'name', text='(NEW) Name')
        else:
            col.prop(asset, 'name', text='Name')
            
        if add_remove:
            col = row.column()
            op = col.operator('scene.luxcore_ol_remove_asset', text='', icon=CLEAR, emboss=False)
            op.asset_index = idx


        if asset.show_settings:
            col = box.column(align=True)
            col.prop(asset, 'category')
            if ui_props.advanced_settings:
                col = box.column(align=True)
                col.prop(asset, 'url', text='URL')
                col = box.column(align=True)
                col.prop(asset, 'hash')
                if ui_props.asset_type == 'MODEL':
                    col = box.column(align=True)
                    col.prop(asset, 'bbox_min')
                    col = box.column(align=True)
                    col.prop(asset, 'bbox_max')

            col = box.column(align=True)
            col.label(text='Thumbnail:')
            col.prop(asset, 'show_thumbnail', icon=IMAGE)

            if asset.show_thumbnail:
                col.template_ID_preview(asset, 'thumbnail', open='image.open')
            else:
                col.template_ID(asset, 'thumbnail', open='image.open')

            col = box.column(align=True)
            col.enabled = (asset.thumbnail is not None)
            
            if not add_remove:
                op = col.operator('scene.luxcore_ol_add_asset', text='Add asset')
                op.asset_index = idx
                 
    
class LuxCoreOnlineLibraryEditAsset(PropertyGroup):
    username : StringProperty(name='Username', description='Username for FTP Server Login', default='', options={'SKIP_SAVE'})
    password : StringProperty(name='Password', description='Password for FTP Server Login', default='', subtype='PASSWORD',  options={'SKIP_SAVE'})
    repopath : StringProperty(name='Repository', description='Git Repository Directory', subtype='DIR_PATH', update=update_repopath)
    filepath : StringProperty(name='Filepath', description='Directory with new assets', subtype='DIR_PATH', update=update_filepath)
    git_repo : BoolProperty(default=False)
    gitclone : BoolProperty(default=False)
    show_assets : BoolProperty(default=False)
    show_new_assets : BoolProperty(default=False)
    progress_info : StringProperty(name='progress_info', description='Uprogress_info', default='', options={'SKIP_SAVE'})
    messages = []
    
    
    asset_items = [
        ('MODEL', 'Model', 'Browse models', 'OBJECT_DATAMODE', 0),
        # ('SCENE', 'SCENE', 'Browse scenes', 'SCENE_DATA', 1),
        ('MATERIAL', 'Material', 'Browse materials', 'MATERIAL', 2),
    ]
    
    asset_type: EnumProperty(name='Active Asset Type', items=asset_items, description='Activate asset in UI',
                             default='MODEL', update=switch_assettype)
                             
    
    asset_sortitems = [
        ('NAME', 'Name', 'CATEGORY', '', 0),
        ('CATEGORY', 'Category', 'Category', '', 1),
        #('DATE', 'Date', 'Date', '', 2),
    ]
    
    asset_sorttype: EnumProperty(name='Asset Sort Type', items=asset_sortitems, description='Sort assets by ...',
                             default='NAME', update=switch_assetsorttype)
    
    
    assets: CollectionProperty(type=LuxCoreOnlineLibraryAsset)
    blendermarket_assets: BoolProperty(default=False, update=switch_blendermarket)
    new_assets: CollectionProperty(type=LuxCoreOnlineLibraryAsset)
    remove_assets: CollectionProperty(type=LuxCoreOnlineLibraryAsset)
    advanced_settings: BoolProperty(default=False)


def register():
    bpy.utils.register_class(VIEW3D_PT_LUXCORE_ONLINE_LIBRARY_EDIT_ASSETS)
    bpy.utils.register_class(LOLUploadTOCOperator)
    bpy.utils.register_class(LOLAddAssetOperator)
    bpy.utils.register_class(LOLAddAllAssetOperator)
    bpy.utils.register_class(LOLCheckPathOperator)
    bpy.utils.register_class(LOLRemoveAssetOperator)
    bpy.utils.register_class(LOLClearMessagesOperator)
    bpy.utils.register_class(LOLUpdateGitRepositoy)
    bpy.utils.register_class(LOLCloneGitRepositoy)


def unregister():
    bpy.utils.unregister_class(VIEW3D_PT_LUXCORE_ONLINE_LIBRARY_EDIT_ASSETS)
    bpy.utils.unregister_class(LuxCoreOnlineLibraryEditAsset)
    bpy.utils.unregister_class(LOLUploadTOCOperator)
    bpy.utils.unregister_class(LuxCoreOnlineLibraryAsset)
    bpy.utils.unregister_class(LOLAddAssetOperator)
    bpy.utils.unregister_class(LOLAddAllAssetOperator)
    bpy.utils.unregister_class(LOLCheckPathOperator)
    bpy.utils.unregister_class(LOLRemoveAssetOperator)
    bpy.utils.unregister_class(LOLClearMessagesOperator)
    bpy.utils.unregister_class(LOLUpdateGitRepositoy)
    bpy.utils.unregister_class(LOLLoadTOCfromGitRepositoy)
    bpy.utils.unregister_class(LOLCloneGitRepositoy)
   
######################################################################################################################

# Register Properties
bpy.utils.register_class(LuxCoreOnlineLibraryAsset)
bpy.utils.register_class(LuxCoreOnlineLibraryEditAsset)
bpy.utils.register_class(LOLLoadTOCfromGitRepositoy)
bpy.types.Scene.editAsset = PointerProperty(type=LuxCoreOnlineLibraryEditAsset)    

ui_props = bpy.context.scene.editAsset
user_preferences = bpy.context.preferences.addons['BlendLuxCore'].preferences
ui_props.username = ''
ui_props.password = ''
ui_props.progress_info = ''
ui_props.git_repo = False
ui_props.gitclone = False

edit_assets_prop = bpy.context.scene.editAsset.assets
edit_assets_prop.clear()
        
new_assets_prop = bpy.context.scene.editAsset.new_assets
new_assets_prop.clear()

remove_assets_prop = bpy.context.scene.editAsset.remove_assets
remove_assets_prop.clear()

register()
if exists(join(ui_props.repopath,'.git')):
    ui_props.git_repo = True
    ui_props.progress_info = 'Updating repository'
    
    chdir(ui_props.repopath)
    process = subprocess.Popen('git-lfs fetch', stdout=subprocess.PIPE)
    process.wait()
    
    process = subprocess.Popen('git-lfs checkout', stdout=subprocess.PIPE)
    process.wait()
    ui_props.progress_info = ''
    chdir('..')
    
    bpy.ops.scene.luxcore_ol_load_toc_from_git_repository()
