#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
from torch import nn
import numpy as np
from utils.graphics_utils import getWorld2View2, getProjectionMatrix
from utils.general_utils import get_args, get_log_file
import utils.general_utils as utils
import time

class Camera(nn.Module):
    def __init__(self, colmap_id, R, T, FoVx, FoVy, image, gt_alpha_mask,
                 image_name, uid,
                 trans=np.array([0.0, 0.0, 0.0]), scale=1.0, data_device = "cuda"
                 ):
        super(Camera, self).__init__()

        self.uid = uid
        self.colmap_id = colmap_id
        self.R = R
        self.T = T
        self.FoVx = FoVx
        self.FoVy = FoVy
        self.image_name = image_name

        args = get_args()
        log_file = get_log_file()
        try:
            if args.lazy_load_image:
                self.data_device = torch.device("cpu")
            else:
                self.data_device = torch.device(data_device)
        except Exception as e:
            print(e)
            print(f"[Warning] Custom device {data_device} failed, fallback to default cuda device" )
            self.data_device = torch.device("cuda")

        if args.time_image_loading:
            if not args.lazy_load_image:
                torch.cuda.synchronize()
            start_time = time.time()

        if (args.distributed_dataset_storage and utils.LOCAL_RANK == 0) or (not args.distributed_dataset_storage):
            # load to cpu
            self.original_image_cpu = image.contiguous()
            self.image_width = self.original_image_cpu.shape[2]
            self.image_height = self.original_image_cpu.shape[1]
            # TODO: fix this later.
            assert gt_alpha_mask is None, "gt_alpha_mask should be None if image is loaded"
            # if gt_alpha_mask is not None:
            #     self.original_image *= gt_alpha_mask.to(self.data_device)
            # else:
            #     self.original_image *= torch.ones((1, self.image_height, self.image_width), device=self.data_device)
        else:
            self.original_image_cpu = None
            self.image_height, self.image_width = utils.get_img_size()

        if args.time_image_loading:
            if not args.lazy_load_image:
                torch.cuda.synchronize()
            log_file.write(f"Image processing in {time.time() - start_time} seconds\n")

        self.zfar = 100.0
        self.znear = 0.01

        self.trans = trans
        self.scale = scale

        self.world_view_transform = torch.tensor(getWorld2View2(R, T, trans, scale)).transpose(0, 1).cuda()
        self.projection_matrix = getProjectionMatrix(znear=self.znear, zfar=self.zfar, fovX=self.FoVx, fovY=self.FoVy).transpose(0,1).cuda()
        self.full_proj_transform = (self.world_view_transform.unsqueeze(0).bmm(self.projection_matrix.unsqueeze(0))).squeeze(0)
        self.camera_center = self.world_view_transform.inverse()[3, :3]

class MiniCam:
    def __init__(self, width, height, fovy, fovx, znear, zfar, world_view_transform, full_proj_transform):
        self.image_width = width
        self.image_height = height    
        self.FoVy = fovy
        self.FoVx = fovx
        self.znear = znear
        self.zfar = zfar
        self.world_view_transform = world_view_transform
        self.full_proj_transform = full_proj_transform
        view_inv = torch.inverse(self.world_view_transform)
        self.camera_center = view_inv[3][:3]

