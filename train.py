import torch
import argparse
import sys
import json
import shutil

from nerf.provider import NeRFDataset
from nerf.utils import *

from nerf.gui import NeRFGUI
import boto3
# from utils.general import get_config, load_params, get_params_path

import re
from datetime import datetime
import os
def upload_to_s3(local_folder, bucket_name, workspace):
    s3 = boto3.client("s3")
    
    for root, dirs, files in os.walk(local_folder):
        for file in files:
            if file.endswith("_depth.mp4"):
                s3_folder = "stable-dreamfusion/videos/depth/"
                new_file_name = f"{workspace}_depth.mp4"
            elif file.endswith("_rgb.mp4"):
                s3_folder = "stable-dreamfusion/videos/rgb/"
                new_file_name = f"{workspace}_rgb.mp4"
            else:
                continue

            local_file = os.path.join(root, file)
            s3_key = os.path.join(s3_folder, new_file_name)

            try:
                s3.upload_file(local_file, bucket_name, s3_key)
                print(f"Uploaded {local_file} to s3://{bucket_name}/{s3_key}")
            except Exception as e:
                print(f"Error uploading {local_file} to S3: {e}")

def append_attributes_to_file(file_path):
    attributes = {}
    with open(file_path, 'r') as file:
        content = file.read()

        attributes['workspace'] = re.search(r'Trainer: df \| .+ \| .+ \| .+ \| (.+)', content).group(1).replace('_', ' ')
        attributes['iters'] = int(re.search(r'load at epoch \d+, global step (\d+)', content).group(1))
        attributes['epochs'] = int(re.findall(r'Epoch (\d+)/\d+', content)[-1])
        attributes['checkpoint'] = re.search(r'Latest checkpoint is (.+)', content).group(1).split('/')[-1]

        start_time = re.search(r'\[INFO\] Trainer: df \| (.+?) \|', content).group(1)
        attributes['start_time'] = datetime.strptime(start_time, '%Y-%m-%d_%H-%M-%S')

        end_time = re.findall(r'\[INFO\] Trainer: df \| (.+?) \|', content)[-1]
        attributes['end_time'] = datetime.strptime(end_time, '%Y-%m-%d_%H-%M-%S')
        attributes['time'] = attributes['end_time'] - attributes['start_time']

        attributes['duration'] = float(re.search(r'training takes (\d+.\d+) minutes', content).group(1))

        start_lr = re.search(r'Start Training .+ Epoch 1/\d+, lr=(\d+\.\d+)', content).group(1)
        attributes['start_lr'] = float(start_lr)
        end_lr = re.search(r'Start Training .+ Epoch '+str(attributes['epochs'])+'/\d+, lr=(\d+\.\d+)', content).group(1)
        attributes['end_lr'] = float(end_lr)

    with open(file_path, 'a') as file:
        file.write('\n\nAttributes from file:\n')
        for key, value in attributes.items():
            file.write(f"{key}: {value}\n")

    return attributes



def copy_directory(src, dst):
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        src_item = os.path.join(src, item)
        dst_item = os.path.join(dst, item)

        if os.path.isdir(src_item):
            copy_directory(src_item, dst_item)
        else:
            shutil.copy2(src_item, dst_item)

# torch.autograd.set_detect_anomaly(True)
def train(opt):
    if opt.O:
        opt.fp16 = True
        opt.dir_text = True
        opt.cuda_ray = True

    elif opt.O2:
        # only use fp16 if not evaluating normals (else lead to NaNs in training...)
        # if opt.albedo:
        #     opt.fp16 = True
        opt.fp16 = True
        opt.dir_text = True
        opt.backbone = 'vanilla'

    if opt.albedo:
        opt.albedo_iters = opt.iters

    if opt.backbone == 'vanilla':
        from nerf.network import NeRFNetwork
    elif opt.backbone == 'grid':
        from nerf.network_grid import NeRFNetwork
    elif opt.backbone == 'grid_taichi':
        opt.cuda_ray = False
        opt.taichi_ray = True
        import taichi as ti
        from nerf.network_grid_taichi import NeRFNetwork
        taichi_half2_opt = True
        taichi_init_args = {"arch": ti.cuda, "device_memory_GB": 4.0}
        if taichi_half2_opt:
            taichi_init_args["half2_vectorization"] = True
        ti.init(**taichi_init_args)
    else:
        raise NotImplementedError(f'--backbone {opt.backbone} is not implemented!')
    
    print(opt)


    seed_everything(opt.seed)

    model = NeRFNetwork(opt)

    print(model)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if opt.test:
        guidance = None # no need to load guidance model at test

        trainer = Trainer(' '.join(sys.argv), 'df', opt, model, guidance, device=device, workspace=opt.workspace, fp16=opt.fp16, use_checkpoint=opt.ckpt)

        if opt.gui:
            gui = NeRFGUI(opt, trainer)
            gui.render()
        
        else:
            test_loader = NeRFDataset(opt, device=device, type='test', H=opt.H, W=opt.W, size=100).dataloader()
            trainer.test(test_loader)
            
            if opt.save_mesh:
                # a special loader for poisson mesh reconstruction, 
                # loader = NeRFDataset(opt, device=device, type='test', H=128, W=128, size=100).dataloader()
                trainer.save_mesh()
    
    else:
        
        train_loader = NeRFDataset(opt, device=device, type='train', H=opt.h, W=opt.w, size=100).dataloader()

        if opt.optim == 'adan':
            from optimizer import Adan
            # Adan usually requires a larger LR
            optimizer = lambda model: Adan(model.get_params(10 * opt.lr), eps=1e-8, weight_decay=0.02, max_grad_norm=5.0, foreach=False)
            # optimizer = lambda model: Adan(model.get_params(7.5 * opt.lr), eps=1e-8, weight_decay=2e-3, max_grad_norm=5.0, foreach=False)
        else: # adam
            optimizer = lambda model: torch.optim.Adam(model.get_params(opt.lr), betas=(0.9, 0.99), eps=1e-15)

        if opt.backbone == 'vanilla':
            warm_up_with_cosine_lr = lambda iter: iter / opt.warm_iters if iter <= opt.warm_iters \
                else max(0.5 * ( math.cos((iter - opt.warm_iters) /(opt.iters - opt.warm_iters) * math.pi) + 1), 
                         opt.min_lr / opt.lr)

            scheduler = lambda optimizer: optim.lr_scheduler.LambdaLR(optimizer, warm_up_with_cosine_lr)
        else:
            # scheduler = lambda optimizer: optim.lr_scheduler.LambdaLR(optimizer, lambda iter: 1) # fixed
            scheduler = lambda optimizer: optim.lr_scheduler.LambdaLR(optimizer, lambda iter: 0.1 ** min(iter / opt.iters, 1))

        if opt.guidance == 'stable-diffusion':
            from sd import StableDiffusion
            guidance = StableDiffusion(device, opt.sd_version, opt.hf_key)
        elif opt.guidance == 'clip':
            from nerf.clip import CLIP
            guidance = CLIP(device)
        else:
            raise NotImplementedError(f'--guidance {opt.guidance} is not implemented.')

        trainer = Trainer(' '.join(sys.argv), 'df', opt, model, guidance, device=device, workspace=opt.workspace, optimizer=optimizer, ema_decay=None, fp16=opt.fp16, lr_scheduler=scheduler, use_checkpoint=opt.ckpt, eval_interval=opt.eval_interval, scheduler_update_every_step=True)

        if opt.gui:
            trainer.train_loader = train_loader # attach dataloader to trainer

            gui = NeRFGUI(opt, trainer)
            gui.render()
        
        else:
            valid_loader = NeRFDataset(opt, device=device, type='val', H=opt.H, W=opt.W, size=5).dataloader()
            max_epoch = np.ceil(opt.iters / len(train_loader)).astype(np.int32)
            trainer.train(train_loader, valid_loader, max_epoch)

            # doing the testing just after training is done doing --test// --save_mesh
            guidance = None # no need to load guidance model at test
            trainer = Trainer(' '.join(sys.argv), 'df', opt, model, guidance, device=device, workspace=opt.workspace, fp16=opt.fp16, use_checkpoint=opt.ckpt)
            test_loader = NeRFDataset(opt, device=device, type='test', H=opt.H, W=opt.W, size=100).dataloader()
            trainer.test(test_loader)
            # a special loader for poisson mesh reconstruction, 
            # loader = NeRFDataset(opt, device=device, type='test', H=128, W=128, size=100).dataloader()
            trainer.save_mesh()

            # # Check if running in SageMaker
            # if 'SM_MODEL_DIR' in os.environ:
            print("Saving all folders and files to to /opt/ml/.")
            # Copy the entire directory structure from self.ckpt_path to /opt/ml/model
            copy_directory(opt.workspace, '/opt/ml/model')


            # instead of zip everything together, expose some for demo purpose
            # import boto3
            s3 = boto3.client('s3')
            workspace = opt.workspace
            s3_bucket = 'jerry-3d-object-generation'
            s3_folder = f"stable-dreamfusion/results/{workspace}"
            local_mesh_folder = os.path.join(workspace, "mesh")

            files_to_upload = ['mesh.obj', 'mesh.mtl', 'albedo.png']
            if opt.albedo:
                files_to_upload = ['mesh.obj']

            for file in files_to_upload:
                s3.upload_file(
                    os.path.join(local_mesh_folder, file),
                    s3_bucket,
                    f"{s3_folder}/{file}"
                )
            

            local_videos_folder = os.path.join(workspace, "results")
            bucket_name = "jerry-3d-object-generation"
            upload_to_s3(local_videos_folder, bucket_name, workspace)

            attribute = append_attributes_to_file(os.path.join(workspace, 'log_df.txt'))
            s3.upload_file(
                    os.path.join(workspace, 'log_df.txt'),
                    s3_bucket,
                    f"stable-dreamfusion/logs/{workspace}.txt"
                )
            
            # upload all the best images
            image_string = attribute['checkpoint'].split('.')[0]
            for root, dirs, files in os.walk(os.path.join(workspace, "validation")):
                for file in files:
                    if file.startswith(image_string):
                        local_file = os.path.join(root, file)
                        s3_key = f"stable-dreamfusion/images/rgb/{workspace}/{file}"
                        if file.endswith("_depth.png"):
                            s3_key = f"stable-dreamfusion/images/depth/{workspace}/{file}"
                        try:
                            s3.upload_file(local_file, bucket_name, s3_key)
                            print(f"Uploaded {local_file} to s3://{bucket_name}/{s3_key}")
                        except Exception as e:
                            print(f"Error uploading {local_file} to S3: {e}")



def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--text', default=None, help="text prompt")
    parser.add_argument('--negative', default='', type=str, help="negative text prompt")
    parser.add_argument('-O', action='store_true', help="equals --fp16 --cuda_ray --dir_text")
    parser.add_argument('-O2', action='store_true', help="equals --backbone vanilla --dir_text")
    parser.add_argument('--test', action='store_true', help="test mode")
    parser.add_argument('--eval_interval', type=int, default=1, help="evaluate on the valid set every interval epochs")
    parser.add_argument('--workspace', type=str, default='workspace')
    parser.add_argument('--guidance', type=str, default='stable-diffusion', help='choose from [stable-diffusion, clip]')
    parser.add_argument('--seed', type=int, default=0)

    parser.add_argument('--save_mesh', action='store_true', help="export an obj mesh with texture")
    parser.add_argument('--mcubes_resolution', type=int, default=256, help="mcubes resolution for extracting mesh")
    parser.add_argument('--decimate_target', type=int, default=1e5, help="target face number for mesh decimation")

    ### training options
    parser.add_argument('--iters', type=int, default=10000, help="training iters")
    parser.add_argument('--lr', type=float, default=1e-3, help="max learning rate")
    parser.add_argument('--warm_iters', type=int, default=500, help="training iters")
    parser.add_argument('--min_lr', type=float, default=1e-4, help="minimal learning rate")
    parser.add_argument('--ckpt', type=str, default='latest')
    parser.add_argument('--cuda_ray', action='store_true', help="use CUDA raymarching instead of pytorch")
    parser.add_argument('--taichi_ray', action='store_true', help="use taichi raymarching")
    parser.add_argument('--max_steps', type=int, default=1024, help="max num steps sampled per ray (only valid when using --cuda_ray)")
    parser.add_argument('--num_steps', type=int, default=64, help="num steps sampled per ray (only valid when not using --cuda_ray)")
    parser.add_argument('--upsample_steps', type=int, default=32, help="num steps up-sampled per ray (only valid when not using --cuda_ray)")
    parser.add_argument('--update_extra_interval', type=int, default=16, help="iter interval to update extra status (only valid when using --cuda_ray)")
    parser.add_argument('--max_ray_batch', type=int, default=4096, help="batch size of rays at inference to avoid OOM (only valid when not using --cuda_ray)")
    parser.add_argument('--albedo', action='store_true', help="only use albedo shading to train, overrides --albedo_iters")
    parser.add_argument('--albedo_iters', type=int, default=1000, help="training iters that only use albedo shading")
    parser.add_argument('--jitter_pose', action='store_true', help="add jitters to the randomly sampled camera poses")
    parser.add_argument('--uniform_sphere_rate', type=float, default=0.5, help="likelihood of sampling camera location uniformly on the sphere surface area")
    # model options
    parser.add_argument('--bg_radius', type=float, default=1.4, help="if positive, use a background model at sphere(bg_radius)")
    parser.add_argument('--density_activation', type=str, default='softplus', choices=['softplus', 'exp'], help="density activation function")
    parser.add_argument('--density_thresh', type=float, default=0.1, help="threshold for density grid to be occupied")
    parser.add_argument('--blob_density', type=float, default=10, help="max (center) density for the density blob")
    parser.add_argument('--blob_radius', type=float, default=0.5, help="control the radius for the density blob")
    # network backbone
    parser.add_argument('--fp16', action='store_true', help="use amp mixed precision training")
    parser.add_argument('--backbone', type=str, default='grid', choices=['grid', 'vanilla', 'grid_taichi'], help="nerf backbone")
    parser.add_argument('--optim', type=str, default='adan', choices=['adan', 'adam'], help="optimizer")
    parser.add_argument('--sd_version', type=str, default='2.1', choices=['1.5', '2.0', '2.1'], help="stable diffusion version")
    parser.add_argument('--hf_key', type=str, default=None, help="hugging face Stable diffusion model key")
    # rendering resolution in training, decrease this if CUDA OOM.
    parser.add_argument('--w', type=int, default=128, help="render width for NeRF in training")
    parser.add_argument('--h', type=int, default=128, help="render height for NeRF in training")
    
    ### dataset options
    parser.add_argument('--bound', type=float, default=1, help="assume the scene is bounded in box(-bound, bound)")
    parser.add_argument('--dt_gamma', type=float, default=0, help="dt_gamma (>=0) for adaptive ray marching. set to 0 to disable, >0 to accelerate rendering (but usually with worse quality)")
    parser.add_argument('--min_near', type=float, default=0.1, help="minimum near distance for camera")
    parser.add_argument('--radius_range', type=float, nargs='*', default=[1.0, 1.5], help="training camera radius range")
    parser.add_argument('--fovy_range', type=float, nargs='*', default=[40, 70], help="training camera fovy range")
    parser.add_argument('--dir_text', action='store_true', help="direction-encode the text prompt, by appending front/side/back/overhead view")
    parser.add_argument('--suppress_face', action='store_true', help="also use negative dir text prompt.")
    parser.add_argument('--angle_overhead', type=float, default=30, help="[0, angle_overhead] is the overhead region")
    parser.add_argument('--angle_front', type=float, default=60, help="[0, angle_front] is the front region, [180, 180+angle_front] the back region, otherwise the side region.")

    ### regularizations
    parser.add_argument('--lambda_entropy', type=float, default=1e-4, help="loss scale for alpha entropy")
    parser.add_argument('--lambda_opacity', type=float, default=0, help="loss scale for alpha value")
    parser.add_argument('--lambda_orient', type=float, default=1e-2, help="loss scale for orientation")
    parser.add_argument('--lambda_tv', type=float, default=0, help="loss scale for total variation")

    ### GUI options
    parser.add_argument('--gui', action='store_true', help="start a GUI")
    parser.add_argument('--W', type=int, default=800, help="GUI width")
    parser.add_argument('--H', type=int, default=800, help="GUI height")
    parser.add_argument('--radius', type=float, default=3, help="default GUI camera radius from center")
    parser.add_argument('--fovy', type=float, default=60, help="default GUI camera fovy")
    parser.add_argument('--light_theta', type=float, default=60, help="default GUI light direction in [0, 180], corresponding to elevation [90, -90]")
    parser.add_argument('--light_phi', type=float, default=0, help="default GUI light direction in [0, 360), azimuth")
    parser.add_argument('--max_spp', type=int, default=1, help="GUI rendering max sample per pixel")
    
    return parser.parse_args()



if __name__ == '__main__':
    args = parse_args()
    
    json_path = "s3://jerry-3d-object-generation/params/parameters.json"
    # Read parameters.json file from S3
    s3 = boto3.resource('s3')
    s3_bucket, s3_key = json_path.replace('s3://', '').split('/', 1)
    obj = s3.Object(s3_bucket, s3_key)
    params = json.loads(obj.get()['Body'].read().decode('utf-8'))

    print("params", params)

    for key, value in params.items():
        if hasattr(args, key):
            setattr(args, key, value)

    print("text: ", args.text)
    print("O: ", args.O)
    print("iters: ", args.iters)

    # text_dir = params.get('text', args.text)

    # with open(text_dir, 'r') as f:
    #     all_text = f.read()
    
    # args.text = all_text.split("\n")[0]

    print('Final hyperparameters:', args)

    train(args)

    