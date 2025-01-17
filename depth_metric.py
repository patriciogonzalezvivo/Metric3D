import os
import cv2
import sys
CODE_SPACE=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(CODE_SPACE)
import argparse
import torch

try:
    from mmcv.utils import Config, DictAction
except:
    from mmengine import Config, DictAction

import numpy as np
from metric3d.model.monodepth_model import get_configured_monodepth_model
from metric3d.utils.running import load_ckpt
from metric3d.utils.mldb import load_data_info, reset_ckpt_path
from metric3d.utils.visualization import save_val_imgs
from metric3d.utils.unproj_pcd import reconstruct_pcd, save_point_cloud

BAND = "depth_metric"
MODEL="models/convlarge_hourglass_0.3_150_step750k_v1.1.pth"
CONFIG="./metric3d/configs/HourglassDecoder/convlarge.0.3_150.py"
DEVICE = 'cuda' if torch.cuda.is_available else 'cpu'

model = None
cfg = None
data_info = {}


# Load MiDAS v3.1 
def init_model(options=None):
    global model, cfg, data_info

    # os.chdir(CODE_SPACE)
    cfg = Config.fromfile(CONFIG)

    if options is not None:
        cfg.merge_from_dict(options)
    
    # ckpt path
    cfg.load_from = MODEL
    cfg.show_dir = "./"
    cfg.distributed = False

    # load data info
    load_data_info('data_info', data_info=data_info)
    cfg.mldb_info = data_info
    cfg.log_file = 'metric.log'

    # update check point info
    reset_ckpt_path(cfg.model, data_info)

    # build model
    model = get_configured_monodepth_model(cfg)
    
    # config distributed training
    model = torch.nn.DataParallel(model).cuda()
        
    # load ckpt
    model, _,  _, _ = load_ckpt(cfg.load_from, model, strict_match=False)
    model.eval()

    model.to(DEVICE)
    return model


def build_camera_model(H : int, W : int, intrinsics : list) -> np.array:
    """
    Encode the camera intrinsic parameters (focal length and principle point) to a 4-channel map. 
    """
    fx, fy, u0, v0 = intrinsics
    f = (fx + fy) / 2.0
    # principle point location
    x_row = np.arange(0, W).astype(np.float32)
    x_row_center_norm = (x_row - u0) / W
    x_center = np.tile(x_row_center_norm, (H, 1)) # [H, W]

    y_col = np.arange(0, H).astype(np.float32) 
    y_col_center_norm = (y_col - v0) / H
    y_center = np.tile(y_col_center_norm, (W, 1)).T # [H, W]

    # FoV
    fov_x = np.arctan(x_center / (f / W))
    fov_y = np.arctan(y_center / (f / H))

    cam_model = np.stack([x_center, y_center, fov_x, fov_y], axis=2)
    return cam_model


def resize_for_input(image, output_shape, intrinsic, canonical_shape, to_canonical_ratio):
    """
    Resize the input.
    Resizing consists of two processed, i.e. 1) to the canonical space (adjust the camera model); 2) resize the image while the camera model holds. Thus the
    label will be scaled with the resize factor.
    """
    padding = [123.675, 116.28, 103.53]
    h, w, _ = image.shape
    resize_ratio_h = output_shape[0] / canonical_shape[0]
    resize_ratio_w = output_shape[1] / canonical_shape[1]
    to_scale_ratio = min(resize_ratio_h, resize_ratio_w)

    resize_ratio = to_canonical_ratio * to_scale_ratio

    reshape_h = int(resize_ratio * h)
    reshape_w = int(resize_ratio * w)

    pad_h = max(output_shape[0] - reshape_h, 0)
    pad_w = max(output_shape[1] - reshape_w, 0)
    pad_h_half = int(pad_h / 2)
    pad_w_half = int(pad_w / 2)

    # resize
    image = cv2.resize(image, dsize=(reshape_w, reshape_h), interpolation=cv2.INTER_LINEAR)
    # padding
    image = cv2.copyMakeBorder(
        image, 
        pad_h_half, 
        pad_h - pad_h_half, 
        pad_w_half, 
        pad_w - pad_w_half, 
        cv2.BORDER_CONSTANT, 
        value=padding)
    
    # Resize, adjust principle point
    intrinsic[2] = intrinsic[2] * to_scale_ratio
    intrinsic[3] = intrinsic[3] * to_scale_ratio

    cam_model = build_camera_model(reshape_h, reshape_w, intrinsic)
    cam_model = cv2.copyMakeBorder(
        cam_model, 
        pad_h_half, 
        pad_h - pad_h_half, 
        pad_w_half, 
        pad_w - pad_w_half, 
        cv2.BORDER_CONSTANT, 
        value=-1)

    pad=[pad_h_half, pad_h - pad_h_half, pad_w_half, pad_w - pad_w_half]
    label_scale_factor=1/to_scale_ratio
    return image, cam_model, pad, label_scale_factor


def transform_test_data_scalecano(rgb, intrinsic, data_basic):
    """
    Pre-process the input for forwarding. Employ `label scale canonical transformation.'
        Args:
            rgb: input rgb image. [H, W, 3]
            intrinsic: camera intrinsic parameter, [fx, fy, u0, v0]
            data_basic: predefined canonical space in configs.
    """
    canonical_space = data_basic['canonical_space']
    forward_size = data_basic.crop_size
    mean = torch.tensor([123.675, 116.28, 103.53]).float()[:, None, None]
    std = torch.tensor([58.395, 57.12, 57.375]).float()[:, None, None]

    # BGR to RGB
    rgb = cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB)

    ori_h, ori_w, _ = rgb.shape
    ori_focal = (intrinsic[0] + intrinsic[1]) / 2
    canonical_focal = canonical_space['focal_length']

    cano_label_scale_ratio = canonical_focal / ori_focal

    canonical_intrinsic = [
        intrinsic[0] * cano_label_scale_ratio,
        intrinsic[1] * cano_label_scale_ratio,
        intrinsic[2],
        intrinsic[3],
    ]

    # resize
    rgb, cam_model, pad, resize_label_scale_ratio = resize_for_input(rgb, forward_size, canonical_intrinsic, [ori_h, ori_w], 1.0)

    # label scale factor
    label_scale_factor = cano_label_scale_ratio * resize_label_scale_ratio

    rgb = torch.from_numpy(rgb.transpose((2, 0, 1))).float()
    rgb = torch.div((rgb - mean), std)
    rgb = rgb[None, :, :, :].cuda()
    
    cam_model = torch.from_numpy(cam_model.transpose((2, 0, 1))).float()
    cam_model = cam_model[None, :, :, :].cuda()
    cam_model_stacks = [
        torch.nn.functional.interpolate(cam_model, size=(cam_model.shape[2]//i, cam_model.shape[3]//i), mode='bilinear', align_corners=False)
        for i in [2, 4, 8, 16, 32]
    ]
    return rgb, cam_model_stacks, pad, label_scale_factor


def get_prediction(
    model: torch.nn.Module,
    input: torch.tensor,
    cam_model: torch.tensor,
    pad_info: torch.tensor,
    scale_info: torch.tensor,
    gt_depth: torch.tensor,
    normalize_scale: float,
    ori_shape: list=[],
):

    data = dict(
        input=input,
        cam_model=cam_model,
    )
    pred_depth, confidence, output_dict = model.module.inference(data)
    pred_depth = pred_depth.squeeze()
    pred_depth = pred_depth[pad_info[0] : pred_depth.shape[0] - pad_info[1], pad_info[2] : pred_depth.shape[1] - pad_info[3]]
    if gt_depth is not None:
        resize_shape = gt_depth.shape
    elif ori_shape != []:
        resize_shape = ori_shape
    else:
        resize_shape = pred_depth.shape

    pred_depth = torch.nn.functional.interpolate(pred_depth[None, None, :, :], resize_shape, mode='bilinear').squeeze() # to original size
    pred_depth = pred_depth * normalize_scale / scale_info
    if gt_depth is not None:
        pred_depth_scale, scale = align_scale(pred_depth, gt_depth)
    else:
        pred_depth_scale = None
        scale = None

    return pred_depth, pred_depth_scale, scale


def infer(img, normalize=True, options=None):
    global model, cfg, data_info

    if model == None:
        init_model(options)

    normalize_scale = cfg.data_basic.depth_range[1]
    intrinsic = [1000.0, 1000.0, img.shape[1]/2, img.shape[0]/2]
    rgb_input, cam_models_stacks, pad, label_scale_factor = transform_test_data_scalecano(img, intrinsic, cfg.data_basic)
    pred_depth, pred_depth_scale, scale = get_prediction(
        model = model,
        input = rgb_input,
        cam_model = cam_models_stacks,
        pad_info = pad,
        scale_info = label_scale_factor,
        gt_depth = None,
        normalize_scale = normalize_scale,
        ori_shape=[img.shape[0], img.shape[1]],
    )

    prediction = pred_depth.squeeze().cpu().numpy()

    if normalize:
        # Normalization
        depth_min = prediction.min()
        depth_max = prediction.max()

        if depth_max - depth_min > np.finfo("float").eps:
            prediction = (prediction - depth_min) / (depth_max - depth_min)

    prediction = cv2.resize(prediction, (img.shape[2], img.shape[1]))

    return prediction


def main(args):
    
    init_model(args.options)
        
    input_path = args.input
    if args.output == '':
        output_filename = input_path[:-4]
    else:
        output_filename = args.output
    
    
    rgb_origin = cv2.imread(input_path)[:, :, ::-1].copy()

    normalize_scale = cfg.data_basic.depth_range[1]
    intrinsic = [1000.0, 1000.0, rgb_origin.shape[1]/2, rgb_origin.shape[0]/2]
    rgb_input, cam_models_stacks, pad, label_scale_factor = transform_test_data_scalecano(rgb_origin, intrinsic, cfg.data_basic)

    pred_depth, pred_depth_scale, scale = get_prediction(
        model = model,
        input = rgb_input,
        cam_model = cam_models_stacks,
        pad_info = pad,
        scale_info = label_scale_factor,
        gt_depth = None,
        normalize_scale = normalize_scale,
        ori_shape=[rgb_origin.shape[0], rgb_origin.shape[1]],
    )

    rgb_torch = torch.from_numpy(rgb_origin).to(pred_depth.device).permute(2, 0, 1)
    mean = torch.tensor([123.675, 116.28, 103.53]).float()[:, None, None].to(rgb_torch.device)
    std = torch.tensor([58.395, 57.12, 57.375]).float()[:, None, None].to(rgb_torch.device)
    rgb_torch = torch.div((rgb_torch - mean), std)

    save_val_imgs(
        pred_depth,
        rgb_torch,
        output_filename
    )

    # pcd
    pred_depth = pred_depth.detach().cpu().numpy()
    intrinsic = [1000.0, 1000.0, rgb_origin.shape[1]/2, rgb_origin.shape[0]/2]
    pcd = reconstruct_pcd(pred_depth, intrinsic[0], intrinsic[1], intrinsic[2], intrinsic[3])
    save_point_cloud(pcd.reshape((-1, 3)), rgb_origin.reshape(-1, 3), output_filename + '.ply')
    
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train a segmentor')
    parser.add_argument('-input','-i', help='input file', required=True)
    parser.add_argument('-output','-o', help='output file', default='')

    parser.add_argument('--node_rank', type=int, default=0)
    parser.add_argument('--nnodes', type=int, default=1, help='number of nodes')
    parser.add_argument('--options', nargs='+', action=DictAction, help='custom options')
    parser.add_argument('--launcher', choices=['None', 'pytorch', 'slurm', 'mpi', 'ror'], default='slurm', help='job launcher')
    parser.add_argument('--test_data_path', default='None', type=str, help='the path of test data')
    args = parser.parse_args()

    main(args)    