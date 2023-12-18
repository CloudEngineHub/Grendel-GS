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

import os
import torch
import numpy as np
import json
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, network_gui
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state, init_distributed
import utils.general_utils as utils
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False
import torch.distributed as dist
from diff_gaussian_rasterization import get_local_pixel_rect

def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from, log_file):
    log_folder = os.environ['LOG_FOLDER']
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree)
    scene = Scene(dataset, gaussians)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)

    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    # DEBUG: print shape of gaussians to know how much data to communicate. 
    # print("xyz shape: ", gaussians._xyz.shape)
    # print("f_dc shape: ", gaussians._features_dc.shape)
    # print("f_rest shape: ", gaussians._features_rest.shape)
    # print("opacity shape: ", gaussians._opacity.shape)
    # print("scaling shape: ", gaussians._scaling.shape)
    # print("rotation shape: ", gaussians._rotation.shape)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1
    for iteration in range(first_iter, opt.iterations + 1):        
        # DEBUG: early stop
        os.environ['ITERATION'] = str(iteration)

        if network_gui.conn == None:
            network_gui.try_connect()
            # check whether connection is established
            # if network_gui.conn == None:
            #     print("local rank {}: no network_gui connection established".format(utils.LOCAL_RANK))
        while network_gui.conn != None:
            try:
                net_image_bytes = None
                custom_cam, do_training, pipe.convert_SHs_python, pipe.compute_cov3D_python, keep_alive, scaling_modifer = network_gui.receive()
                if custom_cam != None:
                    net_image = render(custom_cam, gaussians, pipe, background, scaling_modifer)["render"]
                    net_image_bytes = memoryview((torch.clamp(net_image, min=0, max=1.0) * 255).byte().permute(1, 2, 0).contiguous().cpu().numpy())
                network_gui.send(net_image_bytes, dataset.source_path)
                if do_training and ((iteration < int(opt.iterations)) or not keep_alive):
                    break
            except Exception as e:
                network_gui.conn = None

        iter_start.record()

        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            log_file.write("reset viewpoint stack\n")
            viewpoint_stack = scene.getTrainCameras().copy()
        camera_id = randint(0, len(viewpoint_stack)-1)
        viewpoint_cam = viewpoint_stack.pop(camera_id)

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg = torch.rand((3), device="cuda") if opt.random_background else background

        render_pkg = render(viewpoint_cam, gaussians, pipe, bg)
        image, viewspace_point_tensor, visibility_filter, radii = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"]

        if utils.WORLD_SIZE > 1:
            torch.distributed.all_reduce(image, op=dist.ReduceOp.SUM)

        # Loss
        gt_image = viewpoint_cam.original_image.cuda()

        # DEBUG: modify the label to let the loss only comes from local tiles.
        # print("gt_image shape: ", gt_image.shape) # torch.Size([3, 545, 980])
        # if utils.WORLD_SIZE > 1:
        #     # set bg_color (shape=(3,)) to position out of [x0, x1]*[y0, y1]
        #     # image shape: (3, 545, 980)
        #     x0, x1, y0, y1 = get_local_pixel_rect(viewpoint_cam.image_width, viewpoint_cam.image_height)
        #     # print("local rank {}: width {} height {} x0 {}, y0 {}, x1 {}, y1 {}".format(utils.LOCAL_RANK, 980, 545, x0, y0, x1, y1))
        #     gt_image[:, :y0, :] = background.reshape(3, 1, 1)
        #     gt_image[:, y1:, :] = background.reshape(3, 1, 1)
        #     gt_image[:, :, :x0] = background.reshape(3, 1, 1)
        #     gt_image[:, :, x1:] = background.reshape(3, 1, 1)# redundany value setting.
        
        # DEBUG: save gt_image and image
        # gt_image_cpu = gt_image.clone().detach().cpu().numpy().tolist()
        # image_cpu = image.clone().detach().cpu().numpy().tolist()
        # with open(log_folder+"/gt_image_"+str(utils.LOCAL_RANK)+"_"+str(utils.WORLD_SIZE)+"_"+str(iteration)+".json", 'w') as f:
        #     json.dump(gt_image_cpu, f)
        # with open(log_folder+"/image_"+str(utils.LOCAL_RANK)+"_"+str(utils.WORLD_SIZE)+"_"+str(iteration)+".json", 'w') as f:
        #     json.dump(image_cpu, f)

        Ll1 = l1_loss(image, gt_image)
        loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image, gt_image))
        # print shape and value of loss
        # save in log folder
        log_file.write("iteration {} image: {} loss: {}\n".format(iteration, viewpoint_cam.image_name, loss.item()))

        loss.backward()

        # DEBUG: save gradient data
        # xyz_grad_data = gaussians._xyz.grad.data.clone().detach().cpu()
        # scaling_grad_data = gaussians._scaling.grad.data.clone().detach().cpu()
        # rotation_grad_data = gaussians._rotation.grad.data.clone().detach().cpu()
        # np.savetxt(log_folder+"/_xyz_grad_"+str(utils.LOCAL_RANK)+"_"+str(utils.WORLD_SIZE)+"_"+str(iteration)+".txt", np.asarray(xyz_grad_data))
        # np.savetxt(log_folder+"/_scaling_grad_"+str(utils.LOCAL_RANK)+"_"+str(utils.WORLD_SIZE)+".txt", np.asarray(scaling_grad_data))
        # np.savetxt(log_folder+"/_rotation_grad_"+str(utils.LOCAL_RANK)+"_"+str(utils.WORLD_SIZE)+".txt", np.asarray(rotation_grad_data))

        if utils.WORLD_SIZE > 1:
            gaussians.sync_gradients()
            dist.all_reduce(radii, op=dist.ReduceOp.MAX)

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log

            if utils.LOCAL_RANK == 0:
                if iteration % 10 == 0:
                    progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                    progress_bar.update(10)
                if iteration == opt.iterations:
                    progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background))
            if (iteration in saving_iterations) and utils.LOCAL_RANK == 0:
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                scene.save(iteration)

            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.005, scene.cameras_extent, size_threshold)
                
                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    gaussians.reset_opacity()

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)

            if (iteration in checkpoint_iterations) and utils.LOCAL_RANK == 0:
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])

    # Set up output folder
    if utils.LOCAL_RANK == 0:
        print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()}, 
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()

def print_all_args(log_file):
    # print all arguments in a readable format, each argument in a line.
    log_file.write("arguments:\n")
    log_file.write("-"*30+"\n")
    for arg in vars(args):
        log_file.write("{}: {}\n".format(arg, getattr(args, arg)))
    log_file.write("-"*30+"\n\n")

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    parser.add_argument("--log_folder", type=str, default = "logs")
    parser.add_argument("--zhx_debug", action='store_true', default=False)
    parser.add_argument("--zhx_time", action='store_true', default=False)
    parser.add_argument("--log_interval", type=int, default=50)
    parser.add_argument("--bench_tile_num", type=int, default=100)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)

    init_distributed()
    print("Local rank: " + str(utils.LOCAL_RANK) + " World size: " + str(utils.WORLD_SIZE))

    os.environ['BENCH_TILE_NUM'] = str(args.bench_tile_num)
    args.log_folder = args.log_folder + "/"+str(args.bench_tile_num)

    args.model_path = args.log_folder + "/model_data"
    # create log folder
    if utils.LOCAL_RANK == 0:
        os.makedirs(args.log_folder, exist_ok = True)
        os.makedirs(args.model_path, exist_ok = True)
    # set log folder to env variable
    os.environ['LOG_FOLDER'] = args.log_folder
    os.environ['LOG_INTERVAL'] = str(args.log_interval)

    os.environ['ZHX_DEBUG'] = "true" if args.zhx_debug else "false"
    os.environ['ZHX_TIME'] = "true" if args.zhx_time else "false"

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training 
    network_gui.init(args.ip, args.port+utils.LOCAL_RANK)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)

    # initialize log file
    log_file = open(args.log_folder+"/python_ws="+str(utils.WORLD_SIZE)+"_rk="+str(utils.LOCAL_RANK)+".log", 'w')
    print_all_args(log_file)

    training(lp.extract(args), op.extract(args), pp.extract(args), args.test_iterations, args.save_iterations, args.checkpoint_iterations, args.start_checkpoint, args.debug_from, log_file)

    # All done
    print("\nTraining complete.")