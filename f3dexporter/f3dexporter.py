#
# licenced into the public domain by https://github.com/aconz2/Fusion360Exporter
# add as a "script" in fusion 360.
#

import adsk.core
import adsk.fusion
import traceback
from pathlib import Path
from datetime import datetime
from typing import NamedTuple, List, Set
from enum import Enum
from dataclasses import dataclass
import hashlib
import re
import typing

log_file = None
log_fh = None

handlers = []

def log(*args):
    print(*args, file=log_fh)
    log_fh.flush()

def init_directory(name):
    directory = Path(name)
    directory.mkdir(exist_ok=True)
    return directory

def init_logging(directory):
    global log_file, log_fh
    log_file = directory / '{:%Y_%m_%d_%H_%M}.txt'.format(datetime.now())
    log_fh = open(log_file, 'w')

class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D, Format.STEP}

class Ctx(NamedTuple):
    folder: Path
    formats: List[Format]
    app: adsk.core.Application
    projects: Set[str]

    def extend(self, other):
        return self._replace(folder=self.folder / other)

class LazyDocument:
    def __init__(self, ctx, file):
        self._ctx = ctx
        self._file = file
        self._document = None

    def open(self):
        if self._document is not None:
            return
        log(f'Opening `{self._file.name}`')
        self._document = self._ctx.app.documents.open(self._file)
        self._document.activate()

    def update(self):
        self._ctx.app.executeTextCommand(u'Commands.Start PLM360DeepRefreshDocumentCommand')

    def close(self):
        if self._document is None:
            return
        log(f'Closing {self._file.name}')
        self._document.close(False)  # don't save changes

    @property
    def design(self):
        return design_from_document(self._document)
    
    @property
    def rootComponent(self):
        return self.design.rootComponent

@dataclass
class Counter:
    saved: int = 0
    skipped: int = 0
    errored: int = 0

    def __add__(self, other):
        return Counter(
            self.saved + other.saved,
            self.skipped + other.skipped,
            self.errored + other.errored,
        )
    def __iadd__(self, other):
        self.saved += other.saved
        self.skipped += other.skipped
        self.errored += other.errored
        return self

def design_from_document(document: adsk.core.Document):
    return adsk.fusion.FusionDocument.cast(document).design

def sanitize_filename(name: str) -> str:
    """
    Remove "bad" characters from a filename. Right now just punctuation that Windows doesn't like
    If any chars are removed, we append _{hash} so that we don't accidentally clobber other files
    since eg `Model 1/2` and `Model 1 2` would otherwise have the same name
    """
    # this list of characters is just from trying to rename a file in Explorer (on Windows)
    # I think the actual requirements are per filesystem and will be different on Mac
    # I'm not sure how other unicode chars are handled
    with_replacement = re.sub(r'[:\\/*?<>|]', ' ', name)
    if name == with_replacement:
        return name
    log(f'filename `{name}` contained bad chars, replacing by `{with_replacement}`')
    hash = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f'{with_replacement}_{hash}'

def export_filename(ctx: Ctx, format: Format, file):
    sanitized = sanitize_filename(file.name)
    name = f'{sanitized}.{format.value}'
    return ctx.folder / name

def export_file(ctx: Ctx, format: Format, file, doc: LazyDocument) -> Counter:
    output_path = export_filename(ctx, format, file)
    # if output_path.exists():
    #     log(f'{output_path} already exists, skipping')
    #     return Counter(skipped=1)

    doc.open()

    # I'm just taking this from here https://github.com/tapnair/apper/blob/master/apper/Fusion360Utilities.py
    # is there a nicer way to do this??
    design = doc.design
    em = design.exportManager

    output_path.parent.mkdir(exist_ok=True, parents=True)
    
    # leaving this ugly, not sure what else there might be to handle per format
    if format == Format.F3D:
        options = em.createFusionArchiveExportOptions(str(output_path))
    elif format == Format.STEP:
        options = em.createSTEPExportOptions(str(output_path))
    else:
        raise Exception(f'Got unknown export format {format}')

    em.execute(options)
    log(f'Saved {output_path}')
    
    return Counter(saved=1)

def visit_file(ctx: Ctx, file, is_dry_run: bool, progress: adsk.core.ProgressDialog) -> Counter:
    log(f'Visiting file {file.name} v{file.versionNumber} . {file.fileExtension}')

    if file.fileExtension != 'f3d':
        log(f'file {file.name} has extension {file.fileExtension} which is not currently handled, skipping')
        return Counter(skipped=1)

    doc = LazyDocument(ctx, file)

    counter = Counter()

    for format in ctx.formats:
        progress.message = "Exporting {} as {}...".format(file.name, format)
        if not is_dry_run:
            try:
                counter += export_file(ctx, format, file, doc)
            except Exception:
                counter.errored += 1
                log(traceback.format_exc())
    
    doc.close()
    return counter

def visit_folder(ctx: Ctx, folder, is_dry_run: bool, progress: adsk.core.ProgressDialog) -> Counter:
    counter = Counter()
        
    if progress.wasCancelled:
        return counter

    progress.message = "Visiting folder {}".format(folder.name)
    log(f'Visiting folder {folder.name}')

    new_ctx = ctx.extend(sanitize_filename(folder.name))

    for file in folder.dataFiles:
        try:
            counter += visit_file(new_ctx, file, is_dry_run, progress)
        except Exception:
            log(f'Got exception visiting file\n{traceback.format_exc()}')
            counter.errored += 1

    progress.progressValue += 1

    for sub_folder in folder.dataFolders:
        counter += visit_folder(new_ctx, sub_folder, is_dry_run, progress)
    
    return counter

def main(ctx: Ctx) -> Counter:
    init_directory(ctx.folder)
    init_logging(ctx.folder)

    counter = Counter()

    ui: adsk.core.UserInterface = ctx.app.userInterface
    progress: adsk.core.ProgressDialog = ui.createProgressDialog()
    progress.isCancelButtonShown =True
    for project in ctx.app.data.dataProjects:
        if project.name in ctx.projects:
            progress.show("Exporting from {} Project".format(project.name), "", 0, 100, 0)
            for sub_folder in project.rootFolder.dataFolders:
                counter += visit_folder(ctx, sub_folder, False, progress)
            break

    return counter

# +---------------------------------------------------------------------------+
# | Fusion Hooks
# +---------------------------------------------------------------------------+

class ExporterCommandCreatedEventHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        try:
            cmd = args.command

            cmd.setDialogInitialSize(600, 400)
            # http://help.autodesk.com/view/fusion360/ENU/?guid=GUID-C1BF7FBF-6D35-4490-984B-11EB26232EAD
            cmd.isExecutedWhenPreEmpted = False

            onExecute = ExporterCommandExecuteHandler()
            cmd.execute.add(onExecute)
            onDestroy = ExporterCommandDestroyHandler()
            cmd.destroy.add(onDestroy)
            handlers.append(onExecute)
            handlers.append(onDestroy)

            inputs = cmd.commandInputs
            
            inputs.addStringValueInput('directory', 'Directory', str(Path.home() / 'Desktop/Fusion360Export'))
            
            drop = inputs.addDropDownCommandInput('file_types', 'Export Types', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            for format in Format:
                drop.listItems.add(format.value, format in DEFAULT_SELECTED_FORMATS)

            drop = inputs.addRadioButtonGroupCommandInput('projects', 'Export Project')
            
            app: adsk.core.Application = adsk.core.Application.get()
            app_data: adsk.core.Data = app.data
            project: adsk.core.DataProject = None
            for project in app_data.dataProjects:
                is_activated: bool = False if app_data.activeProject is None else project.name == app_data.activeProject.name
                drop.listItems.add(project.name, is_activated)

        except:
            adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

class ExporterCommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            adsk.terminate()
        except:
            adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    
    # Dont use yield and don't copy list items, swig wants to delete things
    @staticmethod
    def selected(inputs):
        return [it.name for it in inputs if it.isSelected]

    def notify(self, args):
        try:
            inputs = args.command.commandInputs

            app = adsk.core.Application.get()
            ui = app.userInterface

            ctx = Ctx(
                app = app,
                folder = Path(inputs.itemById('directory').value),
                formats = [FormatFromName[x] for x in self.selected(inputs.itemById('file_types').listItems)],
                projects = set(self.selected(inputs.itemById('projects').listItems)),
            )

            counter = main(ctx)

            ui.messageBox('\n'.join((
                f'Saved {counter.saved} files',
                f'Skipped {counter.skipped} files',
                f'Encountered {counter.errored} errors',
                f'Log file is at {log_file}'
            )))

        except:
            tb = traceback.format_exc()
            adsk.core.Application.get().userInterface.messageBox(f'Log file is at {log_file}\n{tb}')
            if log_fh is not None:
                log(f'Got top level exception\n{tb}')    
        finally:
            if log_fh is not None:
                log_fh.close()

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmd_defs = ui.commandDefinitions
        
        CMD_DEF_ID = 'aconz2_Exporter'
        cmd_def = cmd_defs.itemById(CMD_DEF_ID)
        # This isn't how all the other demo scripts manage the lifecycle, but if we don't delete the old
        # command then we get double inputs when we run a second time
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = cmd_defs.addButtonDefinition(
            CMD_DEF_ID, 
            'Export all the things', 
            'Tooltip',
        )

        cmd_created = ExporterCommandCreatedEventHandler()
        cmd_def.commandCreated.add(cmd_created)
        handlers.append(cmd_created)

        cmd_def.execute()

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
