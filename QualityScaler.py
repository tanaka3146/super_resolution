import multiprocessing
import os.path
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.messagebox
import webbrowser
from timeit import default_timer as timer

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch_directml
from customtkinter import (CTk, 
                           CTkButton, 
                           CTkEntry, 
                           CTkFont, 
                           CTkImage,
                           CTkLabel, 
                           CTkOptionMenu, 
                           CTkScrollableFrame,
                           filedialog, 
                           set_appearance_mode,
                           set_default_color_theme)
from moviepy.editor import VideoFileClip
from moviepy.video.io import ImageSequenceClip
from PIL import Image


app_name  = "QualityScaler"
version   = "2.4"

githubme    = "https://github.com/Djdefrag/QualityScaler"
telegramme  = "https://linktr.ee/j3ngystudio"

AI_models_list = [
                  'BSRGANx4',
                  'BSRNetx4',
                  'RealSR_JPEGx4',
                  'RealSR_DPEDx4', 
                  'RRDBx4', 
                  'ESRGANx4', 
                  'FSSR_JPEGx4',
                  'FSSR_DPEDx4',
                 ]

image_extension_list  = [ '.jpg', '.png', '.bmp', '.tiff' ]
video_extension_list  = [ '.mp4', '.avi', '.webm' ]
interpolation_list    = [ 'Yes', 'No' ]
AI_modes_list         = [ "Half precision", "Full precision" ]

device_list_names    = []
device_list          = []
vram_multiplier      = 0.9
gpus_found           = torch_directml.device_count()
downscale_algorithm  = cv2.INTER_AREA
upscale_algorithm    = cv2.INTER_LINEAR_EXACT

offset_y_options = 0.1125
row0_y           = 0.6
row1_y           = row0_y + offset_y_options
row2_y           = row1_y + offset_y_options
row3_y           = row2_y + offset_y_options

app_name_color = "#DA70D6"
dark_color     = "#080808"

torch.autograd.set_detect_anomaly(False)
torch.autograd.profiler.profile(False)
torch.autograd.profiler.emit_nvtx(False)
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")



# ------------------ AI ------------------

## BSRGAN Architecture

class ResidualDenseBlock_5C(nn.Module):
    def __init__(self, nf=64, gc=32, bias=True):
        super(ResidualDenseBlock_5C, self).__init__()
        self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1, bias=bias)
        self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1, bias=bias)
        self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1, bias=bias)
        self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1, bias=bias)
        self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1, bias=bias)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        self._initialize_weights()

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                if m.bias is not None:
                    m.bias.data.zero_()

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x

class RRDB(nn.Module):
    def __init__(self, nf, gc=32):
        super(RRDB, self).__init__()
        self.RDB1 = ResidualDenseBlock_5C(nf, gc)
        self.RDB2 = ResidualDenseBlock_5C(nf, gc)
        self.RDB3 = ResidualDenseBlock_5C(nf, gc)

    def forward(self, x):
        out = self.RDB1(x)
        out = self.RDB2(out)
        out = self.RDB3(out)
        return out * 0.2 + x

class BSRGAN_Net(nn.Module):
    def __init__(self, in_nc=3, out_nc=3, nf=64, nb=23, gc=32, sf=4):
        super(BSRGAN_Net, self).__init__()
        self.sf = sf

        self.conv_first = nn.Conv2d(in_nc, nf, 3, 1, 1, bias=True)
        self.RRDB_trunk = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
        self.trunk_conv = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.upconv1 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        if sf == 4: 
            self.upconv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.HRconv = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.conv_last = nn.Conv2d(nf, out_nc, 3, 1, 1, bias=True)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        self._initialize_weights()

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                if m.bias is not None:
                    m.bias.data.zero_()

    def forward(self, x):
        fea = self.conv_first(x)
        trunk = self.trunk_conv(self.RRDB_trunk(fea))
        fea = fea + trunk

        fea = self.lrelu(self.upconv1(F.interpolate(fea, scale_factor=2, mode='nearest')))
        if self.sf == 4: 
            fea = self.lrelu(self.upconv2(F.interpolate(fea, scale_factor=2, mode='nearest')))
        out = self.conv_last(self.lrelu(self.HRconv(fea)))

        return out

def prepare_model(selected_AI_model, backend, half_precision):
    model_path = find_by_relative_path("AI" + os.sep + selected_AI_model + ".pth")

    model = BSRGAN_Net(in_nc = 3, 
                        out_nc = 3, 
                        nf = 64, 
                        nb = 23, 
                        gc = 32, 
                        sf = 4)
    
    with torch.no_grad():
        pretrained_model = torch.load(model_path, map_location = torch.device('cpu'))
        model.load_state_dict(pretrained_model, strict = True)
    
    model.eval()

    if half_precision: model = model.half()
    model = model.to(backend, non_blocking = True)

    return model

def AI_enhance(model, image, backend, half_precision):
    image = image.astype(np.float32)

    max_range = 65535 if np.max(image) > 256 else 255
    image /= max_range

    img_mode = 'RGB'
    if len(image.shape) == 2:  # gray image
        img_mode = 'L'
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    elif image.shape[2] == 4:  # RGBA image with alpha channel
        img_mode = 'RGBA'
        alpha = image[:, :, 3]
        image = image[:, :, :3]
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        alpha = cv2.cvtColor(alpha, cv2.COLOR_GRAY2RGB)

    image = torch.from_numpy(np.transpose(image, (2, 0, 1))).float()
    if half_precision:
        image = image.unsqueeze(0).half().to(backend, non_blocking=True)
    else:
        image = image.unsqueeze(0).to(backend, non_blocking=True)

    output = model(image)

    output_img = output.squeeze().float().clamp(0, 1).cpu().numpy()
    output_img = np.transpose(output_img, (1, 2, 0))

    if img_mode == 'L':
        output_img = cv2.cvtColor(output_img, cv2.COLOR_RGB2GRAY)

    if img_mode == 'RGBA':
        alpha = torch.from_numpy(np.transpose(alpha, (2, 0, 1))).float()
        if half_precision:
            alpha = alpha.unsqueeze(0).half().to(backend, non_blocking=True)
        else:
            alpha = alpha.unsqueeze(0).to(backend, non_blocking=True)

        output_alpha = model(alpha)

        output_alpha = output_alpha.squeeze().float().clamp(0, 1).cpu().numpy()
        output_alpha = np.transpose(output_alpha, (1, 2, 0))
        output_alpha = cv2.cvtColor(output_alpha, cv2.COLOR_RGB2GRAY)

        output_img = cv2.cvtColor(output_img, cv2.COLOR_RGB2BGRA)
        output_img[:, :, 3] = output_alpha

    output = (output_img * max_range).round().astype(np.uint16 if max_range == 65535 else np.uint8)

    return output



# Classes and utils -------------------

class Gpu:
    def __init__(self, index, name):
        self.name   = name
        self.index  = index

class ScrollableImagesTextFrame(CTkScrollableFrame):
    def __init__(self, master, command=None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.label_list  = []
        self.button_list = []
        self.file_list   = []

    def get_selected_file_list(self): 
        return self.file_list

    def add_clean_button(self):
        label = CTkLabel(self, text = "")
        button = CTkButton(self, 
                            font  = bold11,
                            text  = "CLEAN", 
                            fg_color   = "#282828",
                            text_color = "#E0E0E0",
                            image    = clear_icon,
                            compound = "left",
                            width    = 85, 
                            height   = 27,
                            corner_radius = 25)
        button.configure(command=lambda: self.clean_all_items())
        button.grid(row = len(self.button_list), column=1, pady=(0, 10), padx = 5)
        self.label_list.append(label)
        self.button_list.append(button)

    def add_item(self, text_to_show, file_element, image = None):
        label = CTkLabel(self, 
                        text  = text_to_show,
                        font  = bold11,
                        image = image, 
                        #fg_color   = "#282828",
                        text_color = "#E0E0E0",
                        compound = "left", 
                        padx     = 10,
                        pady     = 5,
                        corner_radius = 25,
                        anchor   = "center")
                        
        label.grid(row  = len(self.label_list), column = 0, 
                   pady = (3, 3), padx = (3, 3), sticky = "w")
        self.label_list.append(label)
        self.file_list.append(file_element)    

    def clean_all_items(self):
        self.label_list  = []
        self.button_list = []
        self.file_list   = []
        place_up_background()
        place_loadFile_section()

for index in range(gpus_found): 
    gpu = Gpu(index = index, name = torch_directml.device_name(index))
    device_list.append(gpu)
    device_list_names.append(gpu.name)

supported_file_extensions = [
                            '.jpg', '.jpeg', '.JPG', '.JPEG',
                            '.png', '.PNG',
                            '.webp', '.WEBP',
                            '.bmp', '.BMP',
                            '.tif', '.tiff', '.TIF', '.TIFF',
                            '.mp4', '.MP4',
                            '.webm', '.WEBM',
                            '.mkv', '.MKV',
                            '.flv', '.FLV',
                            '.gif', '.GIF',
                            '.m4v', ',M4V',
                            '.avi', '.AVI',
                            '.mov', '.MOV',
                            '.qt', '.3gp', 
                            '.mpg', '.mpeg'
                            ]

supported_video_extensions  = [
                                '.mp4', '.MP4',
                                '.webm', '.WEBM',
                                '.mkv', '.MKV',
                                '.flv', '.FLV',
                                '.gif', '.GIF',
                                '.m4v', ',M4V',
                                '.avi', '.AVI',
                                '.mov', '.MOV',
                                '.qt', '.3gp', 
                                '.mpg', '.mpeg'
                            ]



#  Slice functions -------------------

def split_image_into_tiles(image, num_tiles_x, num_tiles_y):
    img_height, img_width, _ = image.shape

    tile_width  = img_width // num_tiles_x
    tile_height = img_height // num_tiles_y

    tiles = []

    for y in range(num_tiles_y):
        y_start = y * tile_height
        y_end   = (y + 1) * tile_height

        for x in range(num_tiles_x):
            x_start = x * tile_width
            x_end   = (x + 1) * tile_width
            tile    = image[y_start:y_end, x_start:x_end]
            tiles.append(tile)

    return tiles

def combine_tiles_into_image(tiles, 
                             image_target_height, 
                             image_target_width,
                             num_tiles_x, 
                             num_tiles_y):

    tiled_image = np.zeros((image_target_height, image_target_width, 4), dtype = np.uint8)

    for i, tile in enumerate(tiles):
        tile_height, tile_width, _ = tile.shape
        row     = i // num_tiles_x
        col     = i % num_tiles_x
        y_start = row * tile_height
        y_end   = y_start + tile_height
        x_start = col * tile_width
        x_end   = x_start + tile_width
        tiled_image[y_start:y_end, x_start:x_end] = add_alpha_channel(tile)

    return tiled_image

def file_need_tiles(image, tiles_resolution):
    height, width, _ = image.shape

    tile_size = tiles_resolution

    num_tiles_horizontal = (width + tile_size - 1) // tile_size
    num_tiles_vertical = (height + tile_size - 1) // tile_size

    total_tiles = num_tiles_horizontal * num_tiles_vertical

    if total_tiles <= 1:
        return False, 0, 0
    else:
        return True, num_tiles_horizontal, num_tiles_vertical

def add_alpha_channel(tile):
    if tile.shape[2] == 3:  # Check if the tile does not have an alpha channel
        alpha_channel = np.full((tile.shape[0], tile.shape[1], 1), 255, dtype=np.uint8)
        tile = np.concatenate((tile, alpha_channel), axis=2)
    return tile

def fix_tile_shape(tile, tile_upscaled):
    tile_height, tile_width, _ = tile.shape
    target_tile_height = tile_height * 4
    target_tile_width  = tile_width * 4

    tile_upscaled = cv2.resize(tile_upscaled, (target_tile_width, target_tile_height))

    return tile_upscaled

def interpolate_images(starting_image, 
                       upscaled_image, 
                       image_target_height, 
                       image_target_width):
    
    starting_image     = add_alpha_channel(cv2.resize(starting_image, (image_target_width, image_target_height), interpolation = upscale_algorithm))
    upscaled_image     = add_alpha_channel(upscaled_image)
    interpolated_image = cv2.addWeighted(upscaled_image, 0.5, starting_image, 0.5, 0)

    return interpolated_image



# Utils functions ------------------------

def opengithub(): webbrowser.open(githubme, new=1)

def opentelegram(): webbrowser.open(telegramme, new=1)

def image_write(file_path, file_data): cv2.imwrite(file_path, file_data)

def image_read(file_path, flags = cv2.IMREAD_UNCHANGED): return cv2.imread(file_path, flags)

def prepare_output_image_filename(image_path, 
                                  selected_AI_model, 
                                  resize_factor, 
                                  selected_image_extension, 
                                  selected_interpolation):
    
    result_path, _    = os.path.splitext(image_path)
    resize_percentage = str(int(resize_factor * 100)) + "%"

    if selected_interpolation:
        to_append = f"_{selected_AI_model}_{resize_percentage}_interpolated{selected_image_extension}"
    else:
        to_append = f"_{selected_AI_model}_{resize_percentage}{selected_image_extension}"

    result_path += to_append

    return result_path

def prepare_output_video_filename(video_path, 
                                  selected_AI_model, 
                                  resize_factor, 
                                  selected_video_extension,
                                  selected_interpolation):
    
    result_path, _    = os.path.splitext(video_path)
    resize_percentage = str(int(resize_factor * 100)) + "%"

    if selected_interpolation:
        to_append = f"_{selected_AI_model}_{resize_percentage}_interpolated{selected_video_extension}"
    else:
        to_append = f"_{selected_AI_model}_{resize_percentage}{selected_video_extension}"

    result_path += to_append

    return result_path

def create_temp_dir(name_dir):
    if os.path.exists(name_dir): shutil.rmtree(name_dir)
    if not os.path.exists(name_dir): os.makedirs(name_dir, mode=0o777)

def remove_dir(name_dir):
    if os.path.exists(name_dir): shutil.rmtree(name_dir)

def write_in_log_file(text_to_insert):
    log_file_name = app_name + ".log"
    with open(log_file_name,'w') as log_file: 
        os.chmod(log_file_name, 0o777)
        log_file.write(text_to_insert) 
    log_file.close()

def read_log_file():
    log_file_name = app_name + ".log"
    with open(log_file_name,'r') as log_file: 
        os.chmod(log_file_name, 0o777)
        step = log_file.readline()
    log_file.close()
    return step

def find_by_relative_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

def delete_list_of_files(list_to_delete):
    if len(list_to_delete) > 0:
        for to_delete in list_to_delete:
            if os.path.exists(to_delete):
                os.remove(to_delete)

def resize_image(image, resize_factor):
    old_height, old_width, _ = image.shape
    new_width  = int(old_width * resize_factor)
    new_height = int(old_height * resize_factor)

    resized_image = cv2.resize(image, (new_width, new_height), interpolation = downscale_algorithm)
    return resized_image       

def resize_frame(frame, new_width, new_height):
    resized_image = cv2.resize(frame, (new_width, new_height), interpolation = downscale_algorithm)
    return resized_image 

def remove_file(name_file):
    if os.path.exists(name_file): os.remove(name_file)

def show_error(exception):
    import tkinter as tk
    tk.messagebox.showerror(title   = 'Error', 
                            message = 'Upscale failed caused by:\n\n' +
                                        str(exception) + '\n\n' +
                                        'Please report the error on Github.com or Telegram group' +
                                        '\n\nThank you :)')

def extract_frames_from_video(video_path):
    video_frames_list = []
    cap          = cv2.VideoCapture(video_path)
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    # extract frames
    video = VideoFileClip(video_path)
    img_sequence = app_name + "_temp" + os.sep + "frame_%01d" + '.jpg'
    video_frames_list = video.write_images_sequence(img_sequence, 
                                                    verbose = False,
                                                    logger  = None, 
                                                    fps     = frame_rate)
    
    # extract audio
    try: video.audio.write_audiofile(app_name + "_temp" + os.sep + "audio.mp3",
                                    verbose = False,
                                    logger  = None)
    except: pass

    return video_frames_list

def video_reconstruction_by_frames(input_video_path, 
                                   frames_upscaled_list, 
                                   selected_AI_model, 
                                   resize_factor, 
                                   cpu_number,
                                   selected_video_extension, 
                                   selected_interpolation):
    
    # Find original video FPS
    cap          = cv2.VideoCapture(input_video_path)
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    # Choose the appropriate codec
    if selected_video_extension == '.mp4':
        codec     = 'libx264'
    elif selected_video_extension == '.avi':
        codec     = 'png'
    elif selected_video_extension == '.webm':
        codec     = 'libvpx'

    upscaled_video_path = prepare_output_video_filename(input_video_path, 
                                                        selected_AI_model, 
                                                        resize_factor, 
                                                        selected_video_extension,
                                                        selected_interpolation)
    audio_file = app_name + "_temp" + os.sep + "audio.mp3"

    clip = ImageSequenceClip.ImageSequenceClip(frames_upscaled_list, fps = frame_rate)
    if os.path.exists(audio_file) and selected_video_extension != '.webm':
        clip.write_videofile(upscaled_video_path,
                            fps     = frame_rate,
                            audio   = audio_file,
                            codec   = codec,
                            verbose = False,
                            logger  = None,
                            threads = cpu_number)
    else:
        clip.write_videofile(upscaled_video_path,
                             fps     = frame_rate,
                             codec   = codec,
                             verbose = False,
                             logger  = None,
                             threads = cpu_number)  



# Core functions ------------------------

def remove_temp_files():
    remove_dir(app_name + "_temp")
    remove_file(app_name + ".log")

def stop_thread():
    # to stop a thread execution
    stop = 1 + "x"

def stop_upscale_process():
    global process_upscale_orchestrator
    process_upscale_orchestrator.terminate()
    process_upscale_orchestrator.join()

def check_upscale_steps():
    time.sleep(3)
    try:
        while True:
            step = read_log_file()
            if "All files completed" in step:
                info_message.set(step)
                stop_upscale_process()
                remove_temp_files()
                stop_thread()
            elif "Error while upscaling" in step:
                info_message.set("Error while upscaling :(")
                remove_temp_files()
                stop_thread()
            elif "Stopped upscaling" in step:
                info_message.set("Stopped upscaling")
                stop_upscale_process()
                remove_temp_files()
                stop_thread()
            else:
                info_message.set(step)
            time.sleep(2)
    except:
        place_upscale_button()

def update_process_status(actual_process_phase):
    print(f"{actual_process_phase}")
    write_in_log_file(actual_process_phase) 

def stop_button_command():
    stop_upscale_process()
    write_in_log_file("Stopped upscaling") 

def upscale_button_command(): 
    global selected_file_list
    global selected_AI_model
    global selected_interpolation
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global selected_video_extension
    global tiles_resolution
    global resize_factor
    global cpu_number

    global process_upscale_orchestrator

    remove_file(app_name + ".log")
    
    if user_input_checks():
        info_message.set("Loading")
        write_in_log_file("Loading")

        print("=" * 50)
        print("> Starting upscale:")
        print(f"  Files to upscale: {len(selected_file_list)}")
        print(f"  Selected AI model: {selected_AI_model}")
        print(f"  AI half precision: {half_precision}")
        print(f"  Interpolation: {selected_interpolation}")
        print(f"  Selected GPU: {torch_directml.device_name(selected_AI_device)}")
        print(f"  Selected image output extension: {selected_image_extension}")
        print(f"  Selected video output extension: {selected_video_extension}")
        print(f"  Tiles resolution for selected GPU VRAM: {tiles_resolution}x{tiles_resolution}px")
        print(f"  Resize factor: {int(resize_factor * 100)}%")
        print(f"  Cpu number: {cpu_number}")
        print("=" * 50)

        backend = torch.device(torch_directml.device(selected_AI_device))

        place_stop_button()

        process_upscale_orchestrator = multiprocessing.Process(
                                            target = upscale_orchestrator,
                                            args   = (selected_file_list,
                                                     selected_AI_model,
                                                     backend, 
                                                     selected_image_extension,
                                                     tiles_resolution,
                                                     resize_factor,
                                                     cpu_number,
                                                     half_precision,
                                                     selected_video_extension,
                                                     selected_interpolation))
        process_upscale_orchestrator.start()

        thread_wait = threading.Thread(target = check_upscale_steps, 
                                       daemon = True)
        thread_wait.start()

# Images

def get_final_image_shape(image_to_upscale):
    # Calculate final image shape
    image_to_upscale_height, image_to_upscale_width, _ = image_to_upscale.shape
    target_height = image_to_upscale_height * 4
    target_width  = image_to_upscale_width * 4
    
    return target_height, target_width

def upscale_image(image_path, 
                  file_number,
                  AI_model, 
                  selected_AI_model, 
                  backend, 
                  selected_image_extension, 
                  tiles_resolution, 
                  resize_factor, 
                  half_precision,
                  selected_interpolation):
    
    starting_image    = image_read(image_path)
    result_image_path = prepare_output_image_filename(image_path, 
                                                      selected_AI_model, 
                                                      resize_factor, 
                                                      selected_image_extension,
                                                      selected_interpolation)
                                                      
    if resize_factor != 1: image_to_upscale = resize_image(starting_image, resize_factor) 
    else:                  image_to_upscale = starting_image

    image_target_height, image_target_width = get_final_image_shape(image_to_upscale)
    need_tiles, num_tiles_x, num_tiles_y    = file_need_tiles(image_to_upscale, tiles_resolution)

    if need_tiles:
        update_process_status(f"{file_number}. Tiling image in {num_tiles_x * num_tiles_y}")

        tiles_list     = split_image_into_tiles(image_to_upscale, num_tiles_x, num_tiles_y)
        how_many_tiles = len(tiles_list)

        with torch.no_grad():
            for tile_index, tile in enumerate(tiles_list, 0):
                update_process_status(f"{file_number}. Upscaling tiles {tile_index}/{how_many_tiles}")       
                
                tile_upscaled = AI_enhance(AI_model, tile, backend, half_precision)
                tile_upscaled = fix_tile_shape(tile, tile_upscaled)
                tiles_list[tile_index] = tile_upscaled

            update_process_status(f"{file_number}. Reconstructing image by tiles")
            image_upscaled = combine_tiles_into_image(tiles_list, 
                                                      image_target_height, 
                                                      image_target_width,
                                                      num_tiles_x, 
                                                      num_tiles_y)
    
    else:
        with torch.no_grad():
            update_process_status(f"{file_number}. Upscaling image")
            image_upscaled = AI_enhance(AI_model, image_to_upscale, backend, half_precision)
            
    if selected_interpolation:
        image_upscaled = interpolate_images(starting_image, image_upscaled, image_target_height, image_target_width)
        image_write(result_image_path, image_upscaled)
    else: 
        image_write(result_image_path, image_upscaled)

# Videos

def get_resized_frame_shape(first_frame, resize_factor):
    height, width, _ = first_frame.shape
    resized_width  = int(width * resize_factor)
    resized_height = int(height * resize_factor)

    return resized_width, resized_height

def get_final_frame_shape(resized_width, resized_height):
    # Calculate final frame shape
    target_height = resized_height * 4
    target_width  = resized_width * 4
    
    return target_height, target_width

def upscale_video(video_path, 
                  file_number,
                  AI_model, 
                  selected_AI_model, 
                  backend, 
                  selected_image_extension, 
                  tiles_resolution,
                  resize_factor, 
                  cpu_number, 
                  half_precision, 
                  selected_video_extension,
                  selected_interpolation):
    
    create_temp_dir(app_name + "_temp")

    update_process_status(f"{file_number}. Extracting video frames")
    frame_list_paths           = extract_frames_from_video(video_path)
    frames_upscaled_paths_list = [] 

    update_process_status(f"{file_number}. Upscaling video")
    first_frame                                = image_read(frame_list_paths[0])  
    frame_resized_width, frame_resized_height  = get_resized_frame_shape(first_frame, resize_factor)
    frame_target_height, frame_target_width    = get_final_frame_shape(frame_resized_width, frame_resized_height)
    need_tiles, num_tiles_x, num_tiles_y       = file_need_tiles(first_frame, tiles_resolution)

    if need_tiles:
        for index_frame, frame_path in enumerate(frame_list_paths, 0):
            
            if (index_frame % 8 == 0): update_process_status(f"{file_number}. Upscaling frame {index_frame}/{len(frame_list_paths)}")
            
            result_frame_path = prepare_output_image_filename(frame_path, selected_AI_model, resize_factor, selected_image_extension, selected_interpolation)
            starting_frame    = image_read(frame_path)

            if resize_factor != 1: frame_to_upscale = resize_frame(starting_frame, frame_resized_width, frame_resized_height)
            else:                  frame_to_upscale = starting_frame

            tiles_list = split_image_into_tiles(frame_to_upscale, num_tiles_x, num_tiles_y)

            with torch.no_grad():
                for tile_index, tile in enumerate(tiles_list, 0):
                    tile_upscaled = AI_enhance(AI_model, tile, backend, half_precision)
                    tile_upscaled = fix_tile_shape(tile, tile_upscaled)
                    tiles_list[tile_index] = tile_upscaled

            frame_upscaled = combine_tiles_into_image(tiles_list, frame_target_height, frame_target_width, num_tiles_x, num_tiles_y)

            if selected_interpolation:
                frame_upscaled = interpolate_images(starting_frame, frame_upscaled, frame_target_height, frame_target_width)
                image_write(result_frame_path, frame_upscaled)
            else: 
                image_write(result_frame_path, frame_upscaled)
            
            frames_upscaled_paths_list.append(result_frame_path)

    else:
        for index_frame, frame_path in enumerate(frame_list_paths, 0):
            if (index_frame % 8 == 0): update_process_status(f"{file_number}. Upscaling frames {index_frame}/{len(frame_list_paths)}")
            
            with torch.no_grad():
                starting_frame    = image_read(frame_path)
                result_frame_path = prepare_output_image_filename(frame_path, selected_AI_model, resize_factor, selected_image_extension, selected_interpolation)
                
                if resize_factor != 1: frame_to_upscale = resize_frame(starting_frame, frame_resized_width, frame_resized_height)
                else:                  frame_to_upscale = starting_frame
                
                frame_upscaled    = AI_enhance(AI_model, frame_to_upscale, backend, half_precision)

                if selected_interpolation:
                    frame_upscaled = interpolate_images(starting_frame, frame_upscaled, frame_target_height, frame_target_width)
                    image_write(result_frame_path, frame_upscaled)
                else: 
                    image_write(result_frame_path, frame_upscaled)

                frames_upscaled_paths_list.append(result_frame_path)

    update_process_status(f"{file_number}. Processing upscaled video")
    video_reconstruction_by_frames(video_path, 
                                   frames_upscaled_paths_list, 
                                   selected_AI_model, 
                                   resize_factor, 
                                   cpu_number, 
                                   selected_video_extension,
                                   selected_interpolation)

def upscale_orchestrator(selected_file_list,
                         selected_AI_model,
                         backend, 
                         selected_image_extension,
                         tiles_resolution,
                         resize_factor,
                         cpu_number,
                         half_precision,
                         selected_video_extension,
                         selected_interpolation):
    
    start = timer()
    torch.set_num_threads(cpu_number)

    try:
        update_process_status("Preparing AI model")
        AI_model = prepare_model(selected_AI_model, backend, half_precision)

        for file_number, file_path in enumerate(selected_file_list, 0):
            file_number = file_number + 1
            update_process_status(f"Upscaling {file_number}/{len(selected_file_list)}")

            if check_if_file_is_video(file_path):
                upscale_video(file_path, 
                              file_number,
                              AI_model, 
                              selected_AI_model, 
                              backend, 
                              selected_image_extension, 
                              tiles_resolution, 
                              resize_factor, 
                              cpu_number, 
                              half_precision, 
                              selected_video_extension,
                              selected_interpolation)
            else:
                upscale_image(file_path, 
                              file_number,
                              AI_model, 
                              selected_AI_model, 
                              backend, 
                              selected_image_extension, 
                              tiles_resolution, 
                              resize_factor, 
                              half_precision, 
                              selected_interpolation)

        update_process_status(f"All files completed ({round(timer() - start)} sec.)")

    except Exception as exception:
        update_process_status('Error while upscaling\n\n' + str(exception))
        show_error(exception)



# GUI utils function ---------------------------

def user_input_checks():
    global selected_file_list
    global selected_AI_model
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global tiles_resolution
    global resize_factor
    global cpu_number

    is_ready = True

    # Selected files -------------------------------------------------
    try: selected_file_list = scrollable_frame_file_list.get_selected_file_list()
    except:
        info_message.set("No file selected. Please select a file")
        is_ready = False

    if len(selected_file_list) <= 0:
        info_message.set("No file selected. Please select a file")
        is_ready = False


    # File resize factor -------------------------------------------------
    try: resize_factor = int(float(str(selected_resize_factor.get())))
    except:
        info_message.set("Resize % must be a numeric value")
        is_ready = False

    if resize_factor > 0: resize_factor = resize_factor/100
    else:
        info_message.set("Resize % must be a value > 0")
        is_ready = False

    
    # Tiles resolution -------------------------------------------------
    try: tiles_resolution = 100 * int(float(str(selected_VRAM_limiter.get())))
    except:
        info_message.set("VRAM/RAM value must be a numeric value")
        is_ready = False 

    if tiles_resolution > 0: 
        selected_vram = (vram_multiplier * int(float(str(selected_VRAM_limiter.get()))))

        if half_precision == True:
            tiles_resolution = int(selected_vram * 100)
        elif half_precision == False:
            tiles_resolution = int(selected_vram * 100 * 0.60)
        
    else:
        info_message.set("VRAM/RAM value must be > 0")
        is_ready = False


    # Cpu number -------------------------------------------------
    try: cpu_number = int(float(str(selected_cpu_number.get())))
    except:
        info_message.set("Cpu number must be a numeric value")
        is_ready = False 

    if cpu_number <= 0:         
        info_message.set("Cpu number value must be > 0")
        is_ready = False
    else: cpu_number = int(cpu_number)


    return is_ready

def extract_image_info(image_file):
    image_name = str(image_file.split("/")[-1])

    image  = image_read(image_file, cv2.IMREAD_UNCHANGED)
    width  = int(image.shape[1])
    height = int(image.shape[0])

    image_label = ( "IMAGE" + " | " + image_name + " | " + str(width) + "x" + str(height) )

    ctkimage = CTkImage(Image.open(image_file), size = (25, 25))

    return image_label, ctkimage

def extract_video_info(video_file):
    cap          = cv2.VideoCapture(video_file)
    width        = round(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height       = round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    num_frames   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    duration     = num_frames/frame_rate
    minutes      = int(duration/60)
    seconds      = duration % 60
    video_name   = str(video_file.split("/")[-1])
    
    while(cap.isOpened()):
        ret, frame = cap.read()
        if ret == False: break
        image_write("temp.jpg", frame)
        break
    cap.release()

    video_label = ( "VIDEO" + " | " + video_name + " | " + str(width) + "x" 
                   + str(height) + " | " + str(minutes) + 'm:' 
                   + str(round(seconds)) + "s | " + str(num_frames) 
                   + "frames | " + str(round(frame_rate)) + "fps" )

    ctkimage = CTkImage(Image.open("temp.jpg"), size = (25, 25))
    
    return video_label, ctkimage

def check_if_file_is_video(file):
    for video_extension in supported_video_extensions:
        if video_extension in file:
            return True

def check_supported_selected_files(uploaded_file_list):
    supported_files_list = []

    for file in uploaded_file_list:
        for supported_extension in supported_file_extensions:
            if supported_extension in file:
                supported_files_list.append(file)

    return supported_files_list

def open_files_action():
    info_message.set("Selecting files...")

    uploaded_files_list = list(filedialog.askopenfilenames())
    uploaded_files_counter = len(uploaded_files_list)

    supported_files_list = check_supported_selected_files(uploaded_files_list)
    supported_files_counter = len(supported_files_list)
    
    print("> Uploaded files: " + str(uploaded_files_counter) + " => Supported files: " + str(supported_files_counter))

    if supported_files_counter > 0:
        place_up_background()

        global scrollable_frame_file_list
        scrollable_frame_file_list = ScrollableImagesTextFrame(master = window, 
                                                               fg_color = dark_color, 
                                                               bg_color = dark_color)
        scrollable_frame_file_list.place(relx = 0.5, 
                                         rely = 0.25, 
                                         relwidth = 1.0, 
                                         relheight = 0.475, 
                                         anchor = tk.CENTER)
        
        scrollable_frame_file_list.add_clean_button()

        for index in range(supported_files_counter):
            actual_file = supported_files_list[index]
            if check_if_file_is_video(actual_file):
                # video
                video_label, ctkimage = extract_video_info(actual_file)
                scrollable_frame_file_list.add_item(text_to_show = video_label, 
                                                    image = ctkimage,
                                                    file_element = actual_file)
                remove_file("temp.jpg")
            else:
                # image
                image_label, ctkimage = extract_image_info(actual_file)
                scrollable_frame_file_list.add_item(text_to_show = image_label, 
                                                    image = ctkimage,
                                                    file_element = actual_file)
    
        info_message.set("Ready")
    else: 
        info_message.set("Not supported files :(")



# GUI select from menus functions ---------------------------

def select_AI_from_menu(new_value: str):
    global selected_AI_model    
    selected_AI_model = new_value

def select_AI_mode_from_menu(new_value: str):
    global half_precision

    if new_value == "Full precision": half_precision = False
    elif new_value == "Half precision": half_precision = True

def select_AI_device_from_menu(new_value: str):
    global selected_AI_device    

    for device in device_list:
        if device.name == new_value:
            selected_AI_device = device.index

def select_image_extension_from_menu(new_value: str):
    global selected_image_extension    
    selected_image_extension = new_value

def select_video_extension_from_menu(new_value: str):
    global selected_video_extension   
    selected_video_extension = new_value

def select_interpolation_from_menu(new_value: str):
    global selected_interpolation
    if new_value == 'Yes':
        selected_interpolation = True
    elif new_value == 'No':
        selected_interpolation = False



# GUI info functions ---------------------------

def open_info_AI_model():
    info = """This widget allows to choose between different AI: \n
- BSRGANx4 | high upscale quality | upscale by 4
- BSRNetx4 | high upscale quality | upscale by 4
- RealSR_JPEGx4 | good upscale quality | upscale by 4
- RealSR_DPEDx4 | good upscale quality | upscale by 4
- RRDBx4 | good upscale quality | upscale by 4
- ESRGANx4 | good upscale quality | upscale by 4
- FSSR_JPEGx4 | good upscale quality | upscale by 4
- FSSR_DPEDx4 | good upscale quality | upscale by 4

Try all AI and choose the one that gives the best results""" 
    
    tk.messagebox.showinfo(title = 'AI model', message = info)

def open_info_device():
    info = """This widget allows to choose the gpu to run AI with. \n 
Keep in mind that the more powerful your gpu is, 
the faster the upscale will be \n
For best results, it is necessary to update the gpu drivers constantly"""

    tk.messagebox.showinfo(title = 'GPU', message = info)

def open_info_file_extension():
    info = """This widget allows to choose the extension of upscaled image/frame:\n
- png | very good quality | supports transparent images
- jpg | good quality | very fast
- bmp | highest quality | slow
- tiff | highest quality | very slow"""

    tk.messagebox.showinfo(title = 'Image output', message = info)

def open_info_resize():
    info = """This widget allows to choose the resolution input to the AI:\n
For example for a 100x100px image:
- Input resolution 50% => input to AI 50x50px
- Input resolution 100% => input to AI 100x100px
- Input resolution 200% => input to AI 200x200px """

    tk.messagebox.showinfo(title = 'Input resolution %', message = info)

def open_info_vram_limiter():
    info = """This widget allows to set a limit on the gpu's VRAM memory usage: \n
- For a gpu with 4 GB of Vram you must select 4
- For a gpu with 6 GB of Vram you must select 6
- For a gpu with 8 GB of Vram you must select 8
- For integrated gpus (Intel-HD series | Vega 3,5,7) 
  that do not have dedicated memory, you must select 2 \n
Selecting a value greater than the actual amount of gpu VRAM may result in upscale failure """

    tk.messagebox.showinfo(title = 'GPU Vram (GB)', message = info)
    
def open_info_cpu():
    info = """This widget allows you to choose how many cpus to devote to the app.\n
Where possible the app will use the number of cpus selected."""

    tk.messagebox.showinfo(title = 'Cpu number', message = info)

def open_info_AI_precision():
    info = """This widget allows you to choose the AI upscaling mode:

- Full precision (>=8GB Vram recommended)
  > compatible with all GPUs 
  > uses 50% more GPU memory than Half precision mode
  > is 30-70% faster than Half precision mode
  
- Half precision
  > some old GPUs are not compatible with this mode
  > uses 50% less GPU memory than Full precision mode
  > is 30-70% slower than Full precision mode"""

    tk.messagebox.showinfo(title = 'AI precision', message = info)

def open_info_video_extension():
    info = """This widget allows you to choose the video output:

- .mp4  | produces good quality and well compressed video
- .avi  | produces the highest quality video
- .webm | produces low quality but light video"""

    tk.messagebox.showinfo(title = 'Video output', message = info)    

def open_info_interpolation():
    info = """This widget allows you to choose interpolating 
the upscaled image/frame with the original image/frame.

- Interpolation allows to increase the quality of the final result, 
  especially when using the tilling/merging function.

- It also increases the the quality of the final result at low 
  "Input resolution %" values (e.g. <50%)."""

    tk.messagebox.showinfo(title = 'Video output', message = info) 



# GUI place functions ---------------------------
        
def place_up_background():
    up_background = CTkLabel(master  = window, 
                            text    = "",
                            fg_color = dark_color,
                            font     = bold12,
                            anchor   = "w")
    
    up_background.place(relx = 0.5, 
                        rely = 0.0, 
                        relwidth = 1.0,  
                        relheight = 1.0,  
                        anchor = tk.CENTER)

def place_github_button():
    git_button = CTkButton(master      = window, 
                            width      = 30,
                            height     = 30,
                            fg_color   = "black",
                            text       = "", 
                            font       = bold11,
                            image      = logo_git,
                            command    = opengithub)
    
    git_button.place(relx = 0.045, rely = 0.87, anchor = tk.CENTER)

def place_telegram_button():
    telegram_button = CTkButton(master     = window, 
                                width      = 30,
                                height     = 30,
                                fg_color   = "black",
                                text       = "", 
                                font       = bold11,
                                image      = logo_telegram,
                                command    = opentelegram)
    telegram_button.place(relx = 0.045, rely = 0.93, anchor = tk.CENTER)
 
def place_stop_button(): 
    stop_button = CTkButton(master   = window, 
                            width      = 140,
                            height     = 30,
                            fg_color   = "#282828",
                            text_color = "#E0E0E0",
                            text       = "STOP", 
                            font       = bold11,
                            image      = stop_icon,
                            command    = stop_button_command)
    stop_button.place(relx = 0.79, rely = row3_y, anchor = tk.CENTER)

def place_loadFile_section():

    text_drop = """ - SUPPORTED FILES -

IMAGES - jpg png tif bmp webp
VIDEOS - mp4 webm mkv flv gif avi mov mpg qt 3gp"""

    input_file_text = CTkLabel(master    = window, 
                                text     = text_drop,
                                fg_color = dark_color,
                                bg_color = dark_color,
                                width   = 300,
                                height  = 150,
                                font    = bold12,
                                anchor  = "center")
    
    input_file_button = CTkButton(master = window, 
                                width    = 140,
                                height   = 30,
                                text     = "SELECT FILES", 
                                font     = bold11,
                                border_spacing = 0,
                                command        = open_files_action)

    input_file_text.place(relx = 0.5, rely = 0.22,  anchor = tk.CENTER)
    input_file_button.place(relx = 0.5, rely = 0.385, anchor = tk.CENTER)

def place_app_name():
    app_name_label = CTkLabel(master     = window, 
                              text       = app_name + " " + version,
                              text_color = app_name_color,
                              font       = bold20,
                              anchor     = "w")
    
    app_name_label.place(relx = 0.21, rely = 0.56, anchor = tk.CENTER)

def place_AI_menu():
    AI_menu_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "AI model",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_AI_model)

    AI_menu = CTkOptionMenu(master  = window, 
                            values  = AI_models_list,
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            anchor     = "center",
                            command    = select_AI_from_menu,
                            dropdown_font = bold11,
                            dropdown_fg_color = "#000000")

    AI_menu_button.place(relx = 0.21, rely = row1_y - 0.05, anchor = tk.CENTER)
    AI_menu.place(relx = 0.21, rely = row1_y, anchor = tk.CENTER)

def place_AI_mode_menu():
    AI_mode_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "AI precision",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_AI_precision)

    AI_mode_menu = CTkOptionMenu(master    = window, 
                                values     = AI_modes_list,
                                width      = 140,
                                font       = bold11,
                                height     = 30,
                                fg_color   = "#000000",
                                anchor     = "center",
                                dynamic_resizing = False,
                                command          = select_AI_mode_from_menu,
                                dropdown_font    = bold11,
                                dropdown_fg_color = "#000000")
    
    AI_mode_button.place(relx = 0.21, rely = row2_y - 0.05, anchor = tk.CENTER)
    AI_mode_menu.place(relx = 0.21, rely = row2_y, anchor = tk.CENTER)

def place_interpolation_menu():
    interpolation_button = CTkButton(master    = window, 
                                    fg_color   = "black",
                                    text_color = "#ffbf00",
                                    text       = "Interpolation",
                                    height     = 23,
                                    width      = 125,
                                    font       = bold11,
                                    corner_radius = 25,
                                    anchor     = "center",
                                    command    = open_info_interpolation)

    interpolation_menu = CTkOptionMenu(master      = window, 
                                        values     = interpolation_list,
                                        width      = 140,
                                        font       = bold10,
                                        height     = 30,
                                        fg_color   = "#000000",
                                        anchor     = "center",
                                        dynamic_resizing = False,
                                        command    = select_interpolation_from_menu,
                                        dropdown_font     = bold11,
                                        dropdown_fg_color = "#000000")
    
    interpolation_button.place(relx = 0.21, rely = row3_y - 0.05, anchor = tk.CENTER)
    interpolation_menu.place(relx = 0.21, rely  = row3_y, anchor = tk.CENTER)

def place_image_extension_menu():
    file_extension_button = CTkButton(master   = window, 
                                    fg_color   = "black",
                                    text_color = "#ffbf00",
                                    text       = "Image output",
                                    height     = 23,
                                    width      = 125,
                                    font       = bold11,
                                    corner_radius = 25,
                                    anchor     = "center",
                                    command    = open_info_file_extension)

    file_extension_menu = CTkOptionMenu(master     = window, 
                                        values     = image_extension_list,
                                        width      = 140,
                                        font       = bold11,
                                        height     = 30,
                                        fg_color   = "#000000",
                                        anchor     = "center",
                                        command    = select_image_extension_from_menu,
                                        dropdown_font = bold11,
                                        dropdown_fg_color = "#000000")
    
    file_extension_button.place(relx = 0.5, rely = row0_y - 0.05, anchor = tk.CENTER)
    file_extension_menu.place(relx = 0.5, rely = row0_y, anchor = tk.CENTER)

def place_video_extension_menu():
    video_extension_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "Video output",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_video_extension)

    video_extension_menu = CTkOptionMenu(master  = window, 
                                    values     = video_extension_list,
                                    width      = 140,
                                    font       = bold11,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    anchor     = "center",
                                    dynamic_resizing = False,
                                    command    = select_video_extension_from_menu,
                                    dropdown_font = bold11,
                                    dropdown_fg_color = "#000000")
    
    video_extension_button.place(relx = 0.5, rely = row1_y - 0.05, anchor = tk.CENTER)
    video_extension_menu.place(relx = 0.5, rely = row1_y, anchor = tk.CENTER)

def place_gpu_menu():
    AI_device_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "GPU",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_device)

    AI_device_menu = CTkOptionMenu(master  = window, 
                                    values   = device_list_names,
                                    width      = 140,
                                    font       = bold9,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    anchor     = "center",
                                    dynamic_resizing = False,
                                    command    = select_AI_device_from_menu,
                                    dropdown_font = bold11,
                                    dropdown_fg_color = "#000000")
    
    AI_device_button.place(relx = 0.5, rely = row2_y - 0.05, anchor = tk.CENTER)
    AI_device_menu.place(relx = 0.5, rely  = row2_y, anchor = tk.CENTER)

def place_vram_textbox():
    vram_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "GPU Vram (GB)",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_vram_limiter)

    vram_textbox = CTkEntry(master      = window, 
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            textvariable = selected_VRAM_limiter)
    
    vram_button.place(relx = 0.5, rely = row3_y - 0.05, anchor = tk.CENTER)
    vram_textbox.place(relx = 0.5, rely  = row3_y, anchor = tk.CENTER)

def place_input_resolution_textbox():
    resize_factor_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "Input resolution (%)",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_resize)

    resize_factor_textbox = CTkEntry(master    = window, 
                                    width      = 140,
                                    font       = bold11,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    textvariable = selected_resize_factor)
    
    resize_factor_button.place(relx = 0.790, rely = row0_y - 0.05, anchor = tk.CENTER)
    resize_factor_textbox.place(relx = 0.790, rely = row0_y, anchor = tk.CENTER)

def place_cpu_textbox():
    cpu_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "CPU number",
                              height   = 23,
                              width    = 125,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_cpu)

    cpu_textbox = CTkEntry(master    = window, 
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            textvariable = selected_cpu_number)

    cpu_button.place(relx = 0.79, rely = row1_y - 0.05, anchor = tk.CENTER)
    cpu_textbox.place(relx = 0.79, rely  = row1_y, anchor = tk.CENTER)

def place_message_label():
    message_label = CTkLabel(master  = window, 
                            textvariable = info_message,
                            height       = 25,
                            font         = bold10,
                            fg_color     = "#ffbf00",
                            text_color   = "#000000",
                            anchor       = "center",
                            corner_radius = 25)
    message_label.place(relx = 0.79, rely = row2_y, anchor = tk.CENTER)

def place_upscale_button(): 
    upscale_button = CTkButton(master    = window, 
                                width      = 140,
                                height     = 30,
                                fg_color   = "#282828",
                                text_color = "#E0E0E0",
                                text       = "UPSCALE", 
                                font       = bold11,
                                image      = play_icon,
                                command    = upscale_button_command)
    upscale_button.place(relx = 0.79, rely = row3_y, anchor = tk.CENTER)
   


class App():
    def __init__(self, window):
        window.title('')
        width        = 675
        height       = 600
        window.geometry("675x600")
        window.minsize(width, height)
        window.iconbitmap(find_by_relative_path("Assets" + os.sep + "logo.ico"))

        place_up_background()

        place_app_name()
        place_github_button()
        place_telegram_button()

        place_AI_menu()
        place_AI_mode_menu()
        place_interpolation_menu()

        place_image_extension_menu()
        place_video_extension_menu()
        place_gpu_menu()
        place_vram_textbox()
        
        place_input_resolution_textbox()
        place_cpu_textbox()
        place_message_label()
        place_upscale_button()

        place_loadFile_section()

if __name__ == "__main__":
    multiprocessing.freeze_support()

    set_appearance_mode("Dark")
    set_default_color_theme("dark-blue")

    window = CTk() 

    global selected_file_list
    global selected_AI_model
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global selected_video_extension
    global selected_interpolation
    global tiles_resolution
    global resize_factor
    global cpu_number

    selected_file_list = []

    if   AI_modes_list[0] == "Half precision": half_precision = True
    elif AI_modes_list[0] == "Full precision": half_precision = False

    selected_interpolation = True
    selected_AI_device     = 0

    selected_AI_model        = AI_models_list[0]
    selected_image_extension = image_extension_list[0]
    selected_video_extension = video_extension_list[0]

    info_message            = tk.StringVar()
    selected_resize_factor  = tk.StringVar()
    selected_VRAM_limiter   = tk.StringVar()
    selected_cpu_number     = tk.StringVar()

    info_message.set("Hi :)")
    selected_resize_factor.set("50")
    selected_VRAM_limiter.set("8")
    selected_cpu_number.set(str(int(os.cpu_count()/2)))

    bold8  = CTkFont(family = "Segoe UI", size = 8, weight = "bold")
    bold9  = CTkFont(family = "Segoe UI", size = 9, weight = "bold")
    bold10 = CTkFont(family = "Segoe UI", size = 10, weight = "bold")
    bold11 = CTkFont(family = "Segoe UI", size = 11, weight = "bold")
    bold12 = CTkFont(family = "Segoe UI", size = 12, weight = "bold")
    bold18 = CTkFont(family = "Segoe UI", size = 18, weight = "bold")
    bold19 = CTkFont(family = "Segoe UI", size = 19, weight = "bold")
    bold20 = CTkFont(family = "Segoe UI", size = 20, weight = "bold")
    bold21 = CTkFont(family = "Segoe UI", size = 21, weight = "bold")

    logo_git      = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "github_logo.png")), size=(15, 15))
    logo_telegram = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "telegram_logo.png")),  size=(15, 15))
    stop_icon     = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "stop_icon.png")), size=(15, 15))
    play_icon     = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "upscale_icon.png")), size=(15, 15))
    clear_icon    = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "clear_icon.png")), size=(15, 15))

    app = App(window)
    window.update()
    window.mainloop()