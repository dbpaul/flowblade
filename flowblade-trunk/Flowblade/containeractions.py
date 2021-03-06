"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor. If not, see <http://www.gnu.org/licenses/>.
"""

import cairo
import hashlib
import mlt
from os import listdir
from os.path import isfile, join
import subprocess
import re
import sys
import threading
import time

import appconsts
import edit
from editorstate import current_sequence
from editorstate import PROJECT
import gmicheadless
import gmicplayer
import respaths
import userfolders
import utils

FULL_RENDER = 0
CLIP_RENDER = 1

OVERLAY_COLOR = (0.17, 0.23, 0.63, 0.5)
GMIC_TYPE_ICON = None

_status_polling_thread = None

# ----------------------------------------------------- interface
def get_action_object(container_data):
    if container_data.container_type == appconsts.CONTAINER_CLIP_GMIC:
        return GMicContainerActions(container_data)

def shutdown_polling():
    if _status_polling_thread == None:
        return
    
    _status_polling_thread.shutdown()
                

# ------------------------------------------------------------ thumbnail creation helpers
def _get_type_icon(container_type):
    global GMIC_TYPE_ICON
    
    if GMIC_TYPE_ICON == None:
        GMIC_TYPE_ICON = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "container_clip_gmic.png")
        
    
    if container_type == appconsts.CONTAINER_CLIP_GMIC:
        return GMIC_TYPE_ICON

def _write_thumbnail_image(profile, file_path):
    """
    Writes thumbnail image from file producer
    """
    # Get data
    md_str = hashlib.md5(file_path.encode('utf-8')).hexdigest()
    thumbnail_path = userfolders.get_cache_dir() + appconsts.THUMBNAILS_DIR + "/" + md_str +  ".png"

    # Create consumer
    consumer = mlt.Consumer(profile, "avformat", 
                                 thumbnail_path)
    consumer.set("real_time", 0)
    consumer.set("vcodec", "png")

    # Create one frame producer
    producer = mlt.Producer(profile, str(file_path))
    if producer.is_valid() == False:
        raise ProducerNotValidError(file_path)

    info = utils.get_file_producer_info(producer)

    length = producer.get_length()
    frame = length // 2
    producer = producer.cut(frame, frame)

    # Connect and write image
    consumer.connect(producer)
    consumer.run()
    
    return (thumbnail_path, length, info)

def _create_image_surface(icon_path):
    icon = cairo.ImageSurface.create_from_png(icon_path)
    scaled_icon = cairo.ImageSurface(cairo.FORMAT_ARGB32, appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT)
    cr = cairo.Context(scaled_icon)
    cr.save()
    cr.scale(float(appconsts.THUMB_WIDTH) / float(icon.get_width()), float(appconsts.THUMB_HEIGHT) / float(icon.get_height()))
    cr.set_source_surface(icon, 0, 0)
    cr.paint()
    cr.restore()

    return (cr, scaled_icon)


# ---------------------------------------------------- action objects
class AbstractContainerActionObject:
    
    def __init__(self, container_data):
        self.container_data = container_data

    def render_full_media(self, clip):
        print("AbstractContainerActionObject.render_full_media not impl")

    def get_session_dir(self):
        return self.get_container_clips_dir() + "/" + self.get_container_program_id()

    def get_rendered_media_dir(self):
        return self.get_session_dir() + gmicheadless.RENDERED_FRAMES_DIR

    def get_container_program_id(self):
        id_md_str = str(self.container_data.container_type) + self.container_data.program + self.container_data.unrendered_media
        return hashlib.md5(id_md_str.encode('utf-8')).hexdigest()

    def get_container_clips_dir(self):
        return userfolders.get_data_dir() + appconsts.CONTAINER_CLIPS_DIR

    def add_as_status_polling_object(self):
        global _status_polling_thread
        if _status_polling_thread == None:
            _status_polling_thread = ContainerStatusPollingThread()
            _status_polling_thread.start()
                   
        _status_polling_thread.poll_objects.append(self)

    def remove_as_status_polling_object(self):
        _status_polling_thread.poll_objects.remove(self)

    def get_lowest_numbered_file(self):
        
        frames_info = gmicplayer.FolderFramesInfo(self.get_rendered_media_dir())
        lowest = frames_info.get_lowest_numbered_file()
        highest =  frames_info.get_highest_numbered_file()
        print(lowest, highest)
        return frames_info.get_lowest_numbered_file()
        
        """
        # This will not work if there are two image sequences in the same folder.
        folder = self.get_rendered_media_dir()

        onlyfiles = [ f for f in listdir(folder) if isfile(join(folder,f)) ]
        lowest_number_part = 1000000000
        lowest_file = None
        for f in onlyfiles:
            try:
                if lowest_file == None:
                    number_parts = re.findall("[0-9]+", f)
                    number_part = number_parts[-1] # we want the last number part 
                    number_index = f.find(number_part)
                    path_name_part = f[0:number_index]
                    lowest_file = f
                    
                file_number_part = int(re.findall("[0-9]+", f)[-1]) # -1, we want the last number part

            except:
                continue
            if f.find(path_name_part) == -1:
                # needs to part of same sequence
                continue
            if file_number_part < lowest_number_part:
                lowest_number_part = file_number_part
                lowest_file = f

        if lowest_file == None:
            return None

        return self.get_rendered_media_dir() + "/" + lowest_file
        """
        
    def get_rendered_thumbnail(self):
        print("AbstractContainerActionObject.get_rendered_thumbnail not impl")
        return None
    
    def update_render_status(self):
        print("AbstractContainerActionObject.update_render_status not impl")

    def abort_render(self):
        print("AbstractContainerActionObject.abort_render not impl")
    


class GMicContainerActions(AbstractContainerActionObject):

    def __init__(self, container_data):
        AbstractContainerActionObject.__init__(self, container_data)
        self.render_type = -1 # to be set in methods below
        self.clip = None # to be set in methods below
        
    def render_full_media(self, clip):
        self.render_type = FULL_RENDER
        self.clip = clip
        self._launch_render(clip, 0, self.container_data.unrendered_length)

        self.add_as_status_polling_object()

    def _launch_render(self, clip, range_in, range_out):
        #print("rendering gmic container clip:", self.get_container_program_id(), range_in, range_out)
        gmicheadless.clear_flag_files(self.get_container_program_id())
        #print(range_in, range_out)
        #frames_info = gmicplayer.FolderFramesInfo(self.get_session_dir() + gmicheadless.CLIP_FRAMES_DIR)
        #print(frames_info.full_range_exits(range_in + 1, range_out + 1))
            
        args = ("session_id:" + self.get_container_program_id(), 
                "script:" + self.container_data.program,
                "clip_path:" + self.container_data.unrendered_media,
                "range_in:" + str(range_in),
                "range_out:"+ str(range_out),
                "profile_desc:" + PROJECT().profile.description().replace(" ", "_"))

        # Run with nice to lower priority if requested
        nice_command = "nice -n " + str(10) + " " + respaths.LAUNCH_DIR + "flowbladegmicheadless"
        for arg in args:
            nice_command += " "
            nice_command += arg

        subprocess.Popen([nice_command], shell=True)

    def update_render_status(self):
    
        if gmicheadless.session_render_complete(self.get_container_program_id()) == True:
            self.remove_as_status_polling_object()
            if self.render_type == FULL_RENDER:

                frame_file = self.get_lowest_numbered_file()
                if frame_file == None:
                    # Something is quite wrong, maybe best to just print out message and give up.
                    print("No frame file found for gmic conatainer clip")
                    return

                resource_name_str = utils.get_img_seq_resource_name(frame_file, True)
                resource_path = self.get_rendered_media_dir() + "/" + resource_name_str

                rendered_clip = current_sequence().create_file_producer_clip(resource_path, new_clip_name=None, novalidate=False, ttl=1)
                track, clip_index = current_sequence().get_track_and_index_for_id(self.clip.id)
                if track == None:
                    # clip was removed from timeline
                    # TODO: infowindow?
                    return

                # "old_clip", "new_clip", "track", "index"
                data = {"old_clip":self.clip,
                        "new_clip":rendered_clip,
                        "rendered_media_path":resource_path,
                        "track":track,
                        "index":clip_index}
                action = edit.container_clip_full_render_replace(data)
                action.do_edit()
        else:
            status = gmicheadless.get_session_status(self.get_container_program_id())
            if status != None:
                print(status)
            else:
                print("Miss")

    def abort_render(self):
        print("AbstractContainerActionObject.abort_render not impl")

    def create_icon(self):
        print("kasdkasdkadskadskkk")
        icon_path, length, info = _write_thumbnail_image(PROJECT().profile, self.container_data.unrendered_media)
        cr, surface = _create_image_surface(icon_path)
        cr.rectangle(0, 0, appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT)
        cr.set_source_rgba(*OVERLAY_COLOR)
        cr.fill()
        type_icon = _get_type_icon(appconsts.CONTAINER_CLIP_GMIC)
        cr.set_source_surface(type_icon, 1, 30)
        cr.set_operator (cairo.OPERATOR_OVERLAY)
        cr.paint_with_alpha(0.5)
 
        return (surface, length)
        """
        self.icon = surface
        self.length = length
        self.container_data.unrendered_length = length - 1
        """
        
    def get_rendered_thumbnail(self):
        surface, length = self.create_icon()
        return surface


class ContainerStatusPollingThread(threading.Thread):
    
    def __init__(self):
        self.poll_objects = []
        self.abort = False
        #self.running = False
        threading.Thread.__init__(self)

    def run(self):
        #self.running = True
        
        while self.abort == False:
            for poll_obj in self.poll_objects:
                poll_obj.update_render_status()
                    
                
            time.sleep(1.0)
            

    def shutdown(self):
        for poll_obj in self.poll_objects:
            poll_obj.abort_render()
        
        self.abort = True
        
