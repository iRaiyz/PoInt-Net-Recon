import os
import argparse
import torch
import numpy as np
import torch
import torch.utils.data
import torch.nn as nn
import torch.optim as optim
import time
import datetime
import sys
import wandb
from tqdm import tqdm
from sklearn.neighbors import NearestNeighbors

torch.backends.cudnn.enabled = False
torch.backends.cudnn.deterministic = True
torch.cuda.empty_cache()

from utils import *
from models.model import *
from models.dataset import *

def torch2img(tensor, h, w):
    """
    Convert a PyTorch tensor to a numpy image.

    Parameters:
    tensor (torch.Tensor): The input tensor.
    h (int): The height of the output image.
    w (int): The width of the output image.

    Returns:
    np.ndarray: The resulting image in numpy array format.
    """
    tensor = tensor.squeeze(0).detach().cpu().numpy()
    tensor = tensor.reshape(3, h, w)
    img = tensor.transpose(1, 2, 0)
    img = img * 255.
    return img

def alb_smoothness_loss(pred_alb, n_neighbors,binary_mask_file_path = None, include_binary_mask = False, include_chroma_weights=False, luminance=None, chromaticity=None):
    """
    Calculate the albedo smoothness loss for the predicted albedo.

    Parameters:
    pred_alb (torch.Tensor): Predicted albedo tensor.
    n_neighbors (int): Number of neighbors to consider for each point.
    include_chroma_weights (bool): Flag to include chromaticity-based weights in the loss.
    luminance (torch.Tensor): Luminance values for each point.
    chromaticity (torch.Tensor): Chromaticity values for each point.

    Returns:
    torch.Tensor: Calculated albedo smoothness loss.
    """

    batch_size, num_channels, num_points = pred_alb.shape
    total_loss = 0.0
    # Load binary mask if included
    # binary_mask = None
    # if include_binary_mask and binary_mask_file_path:
    #     binary_mask = np.load(binary_mask_file_path)

    for b in range(batch_size):
        # Reshape the tensor to two dimensions [num_points, num_channels] for NearestNeighbors
        pred_alb_np = pred_alb[b].detach().cpu().numpy().T  # Transpose to get shape [num_points, num_channels]
        # Apply KNN to find n_neighbors for each point
        neigh = NearestNeighbors(n_neighbors=n_neighbors+1)  # +1 because point itself is included
        neigh.fit(pred_alb_np)
        distances, indices = neigh.kneighbors(pred_alb_np)
        
        # Vectorized computation of differences and norms
        diffs = pred_alb_np[indices[:, 1:]] - pred_alb_np[:, np.newaxis]
        norms = np.linalg.norm(diffs, axis=2)

        # Compute weights if needed
        if include_chroma_weights == True:
            # Prepare batched chromaticity and luminance for vectorized computation
            ch_i = chromaticity[b, :, :, np.newaxis]  # Shape: [2, N, 1]
            ch_j = chromaticity[b, :, indices[:, 1:]] # Shape: [2, N, n_neighbors]
            lum_i = luminance[b, :, np.newaxis]       # Shape: [N, 1]
            lum_j = luminance[b, indices[:, 1:]]      # Shape: [N, n_neighbors]
            # Compute weights for each point and its neighbors
            weights = compute_weights_vectorized(ch_i, ch_j, lum_i, lum_j)
        if include_binary_mask:
            weights = binary_mask[b]  # Assuming binary_mask has the same batch dimension
            weighted_norms = weights[:, np.newaxis] * norms  # Apply weights to each neighbor pair
        if include_binary_mask == False and include_chroma_weights == False:
            weights = np.ones_like(norms)

        # Compute the weighted smoothness loss for this point cloud
        norms_tensor = torch.tensor(norms, device=pred_alb.device)
        weights_tensor = weights.clone().detach()
        weighted_norms = weights_tensor * norms_tensor**2
        batch_loss = torch.sum(weighted_norms)
        total_loss += batch_loss

    # Normalize the smoothness loss by the number of points
    total_loss /= batch_size

    # Convert the loss back to a torch tensor and return
    return total_loss

def shading_loss(pred_shd, img, n_neighbors):
    """
    Calculate the shading loss for a given batch of point clouds using vectorized operations.

    Parameters:
    pred_shd (torch.Tensor): Predicted shading tensor with shape [batch_size, 3, N].
    img (torch.Tensor): Image tensor with shape [batch_size, 6, N].
    n_neighbors (int): Number of neighbors to consider for each point.

    Returns:
    torch.Tensor: Calculated shading loss for the batch.
    """
    batch_size, _, num_points = pred_shd.shape
    total_loss = 0.0

    # Extract RGB channels from img
    img_rgb = img[:, 3:, :]  # Shape: [batch_size, 3, N]

    for b in range(batch_size):
        # Reshape for compatibility with KNN
        pred_shd_flat = pred_shd[b].permute(1, 0).detach().cpu().numpy()  # Shape: [N, 3]
        img_rgb_flat = img_rgb[b].permute(1, 0).detach().cpu().numpy()  # Shape: [N, 3]
        neigh = NearestNeighbors(n_neighbors=n_neighbors + 1)
        neigh.fit(img_rgb_flat)
        _, indices = neigh.kneighbors(img_rgb_flat)

        # Vectorized computation of differences and norms
        diffs_shd = pred_shd_flat[indices[:, 1:]] - pred_shd_flat[:, np.newaxis]
        norms_shd = np.linalg.norm(diffs_shd, axis=2)
        diffs_rgb = img_rgb_flat[indices[:, 1:]] - img_rgb_flat[:, np.newaxis]
        norms_rgb = np.linalg.norm(diffs_rgb, axis=2)
        norms_product = (1 / (1 + norms_rgb)) * norms_shd**2

        # Compute shading loss
        batch_loss = np.sum(norms_product)
        total_loss += batch_loss

    # Normalize the total loss by the number of batches
    total_loss /= batch_size
    final_loss = torch.tensor(total_loss, dtype=torch.float32, device=pred_shd.device)

    return final_loss

def train_model(network, train_loader, val_loader, optimizer, criterion, epochs, s1, s2,
         b1, b2, save_model_path, include_loss_recon = True, include_loss_lid=False, include_loss_alb_smoothness = False, include_loss_shading = False, include_chroma_weights = False, loss_recon_coeff = 1, loss_lid_coeff=1.0, loss_alb_smoothness_coeff = 1.0, loss_shading_coeff = 1.0, wandb_activation = False, early_stopping_patience=3, early_stopping_delta=0):
    """
    Train the PoIntNet model.

    Parameters:
    network (torch.nn.Module): The neural network model.
    train_loader (torch.utils.data.DataLoader): DataLoader for the training dataset.
    optimizer (torch.optim.Optimizer): Optimizer for the training.
    criterion (torch.nn.Module): Loss function.
    epochs (int): Number of epochs to train.
    include_loss_lid (bool): Flag to include loss_lid in the total loss computation.
    loss_lid_coeff (float): Coefficient to scale the impact of loss_lid.

    Returns:
    None
    """
    print("The lidar loss coefficient is {}".format(loss_lid_coeff))
    print("The albedo smoothness loss coefficient is {}".format(loss_alb_smoothness_coeff))
    print("The shading loss coefficient is {}".format(loss_shading_coeff))
    
    best_val_loss = float('inf')
    epochs_without_improvement = 0

    avg_loss_recon, avg_loss_lid, avg_loss_alb, avg_loss_shd = 0.0, 0.0, 0.0, 0.0
    
    alpha = 0.9

    for epoch in range(epochs):
        start_time = time.time()  # Start the timer
        network.train()
        running_loss, running_loss_alb, running_loss_lid, running_loss_shd = 0.0, 0.0, 0.0, 0.0
        
        for data in tqdm(train_loader):
            # Load the data and transfer to GPU
            img, norms, lid, fn = data
            img, norms, lid = img.cuda(), norms.cuda(), lid.cuda()
            # img os size (B,6,N)
            luminance, chromaticity = None, None
            if include_chroma_weights:
                #lum of size (B,1,N) and chroma of size (B,2,N)
                luminance, chromaticity = compute_luminance_and_chromaticity_batched(img)

            # Zero the parameter gradients
            optimizer.zero_grad()

            # Forward pass
            pred_shd, pred_alb = network(img, norms, point_pos_in=1, ShaderOnly=False)
            gray_alb, gray_shd = point_cloud_to_grayscale_torch(pred_alb), point_cloud_to_grayscale_torch(pred_shd)

            # Reconstruct the point cloud
            reconstructed_pcd = reconstruct_image(pred_alb, pred_shd)
            loss = 0

            # Compute loss
            if include_loss_recon:
                loss_recon =  criterion(reconstructed_pcd, img[:,3:6])
                avg_loss_recon = alpha * avg_loss_recon + (1 - alpha) * loss_recon.item()
                loss_recon_scaled = loss_recon_coeff * loss_recon / max(1e-8, avg_loss_recon)
                loss += loss_recon_scaled

            if include_loss_lid:
                epsilon = 0.00001
                lid_normalized = lid / 65535.0
                lid_normalized = torch.squeeze(lid_normalized, 1)

                # Create a mask for non-zero lidar intensity points
                nonzero_mask = lid_normalized != 0

                # Calculate components of loss_lid only for non-zero points
                gray_alb_diff = torch.abs(gray_alb - s1 * lid_normalized - b1)
                gray_shd_diff = torch.abs(gray_shd - s2 * (gray_alb / (lid_normalized + epsilon)) - b2)

                # Apply mask
                gray_alb_diff_masked = gray_alb_diff * nonzero_mask * 10
                gray_shd_diff_masked = gray_shd_diff * nonzero_mask

                # Combine the masked differences to form the loss_lid
                loss_lid = (gray_alb_diff_masked + gray_shd_diff_masked).sum()

                avg_loss_lid = alpha * avg_loss_lid + (1 - alpha) * loss_lid.item()
                loss_lid_scaled = loss_lid_coeff * loss_lid / max(1e-8, avg_loss_lid)
                loss += loss_lid_scaled
                running_loss_lid = loss_lid_scaled

            if include_loss_alb_smoothness:
                loss_alb = alb_smoothness_loss(pred_alb, 10,None,False, include_chroma_weights, luminance, chromaticity)
                avg_loss_alb = alpha * avg_loss_alb + (1 - alpha) * loss_alb.item()
                loss_alb_scaled = loss_alb_smoothness_coeff * loss_alb / max(1e-8, avg_loss_alb)
                loss += loss_alb_scaled
                running_loss_alb += loss_alb_scaled

            if include_loss_shading:
                loss_shd = shading_loss(pred_shd, img, 10)
                avg_loss_shd = alpha * avg_loss_shd + (1 - alpha) * loss_shd.item()
                loss_shd_scaled = loss_shading_coeff * loss_shd / max(1e-8, avg_loss_shd)
                loss += loss_shd_scaled
                running_loss_shd += loss_shd_scaled
            
            loss.backward()
            optimizer.step()
            running_loss += loss.item()

            if wandb_activation:
                wandb.log({"batch_loss": loss.item()})

        epoch_loss = running_loss / len(train_loader)
        epoch_loss_alb = running_loss_alb / len(train_loader)
        epoch_loss_lid = running_loss_lid / len(train_loader)
        epoch_loss_shd = running_loss_shd / len(train_loader)

        if wandb_activation:
            wandb.log({"epoch_train_loss": epoch_loss, "epoch": epoch})
            wandb.log({"epoch_loss_alb": epoch_loss_alb, "epoch": epoch})
            wandb.log({"epoch_loss_lid": epoch_loss_lid, "epoch": epoch})
            wandb.log({"epoch_loss_shd": epoch_loss_shd, "epoch": epoch})
        
        print(f"Epoch {epoch+1}/{epochs}, Loss: {running_loss/len(train_loader)}")
        print(f"Epoch {epoch+1}/{epochs}, Alb Loss: {running_loss_alb/len(train_loader)}")
        print(f"Epoch {epoch+1}/{epochs}, Lid Loss: {running_loss_lid/len(train_loader)}")
        print(f"Epoch {epoch+1}/{epochs}, Shd Loss: {running_loss_shd/len(train_loader)}")
        
        if save_model_path.endswith('.pth'):
            save_model_path = save_model_path[:-4]

        # Ensure the directory exists
        if not os.path.exists(save_model_path):
            os.makedirs(save_model_path)

        if (epoch + 1) % 1 == 0:
            checkpoint_path = os.path.join(save_model_path, f"checkpoint_epoch_{epoch + 1}.pth")
            torch.save({
                'epoch': epoch + 1,
                'model_state_dict': network.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                's1': s1,
                's2': s2,
                'b1': b1,
                'b2': b2,
                'loss': epoch_loss
            }, checkpoint_path)
            print(checkpoint_path)
            print(f"Checkpoint saved at epoch {epoch + 1}")

        # Validation phase
        network.eval()
        val_running_loss, val_running_loss_alb, val_running_loss_lid, val_running_loss_shd = 0.0, 0, 0, 0

        with torch.no_grad():
            for val_data in tqdm(val_loader):
                # Load the validation data and transfer to GPU
                img, norms, lid, _ = val_data
                img, norms, lid = img.cuda(), norms.cuda(), lid.cuda()
                
                luminance, chromaticity = None, None
                if include_chroma_weights:
                    #lum of size (B,1,N) and chroma of size (B,2,N)
                    luminance, chromaticity = compute_luminance_and_chromaticity_batched(img)
                
                # Forward pass for validation
                pred_shd, pred_alb = network(img, norms, point_pos_in=1, ShaderOnly=False)
                gray_alb, gray_shd = point_cloud_to_grayscale_torch(pred_alb), point_cloud_to_grayscale_torch(pred_shd)

                # Reconstruct the point cloud for validation
                reconstructed_pcd = reconstruct_image(pred_alb, pred_shd)
                val_loss = 0

                # Compute validation loss
                if include_loss_recon:
                    val_loss_recon = criterion(reconstructed_pcd, img[:,3:6])
                    val_loss_recon_scaled = loss_recon_coeff * val_loss_recon / max(1e-8, avg_loss_recon)
                    val_loss += val_loss_recon_scaled
                
                if include_loss_lid:
                    epsilon = 0.00001
                    lid_normalized = lid / 65535.0
                    lid_normalized = torch.squeeze(lid_normalized, 1)

                    # Create a mask for non-zero lidar intensity points
                    nonzero_mask = lid_normalized != 0

                    # Calculate components of loss_lid only for non-zero points
                    gray_alb_diff = torch.abs(gray_alb - s1 * lid_normalized - b1)
                    gray_shd_diff = torch.abs(gray_shd - s2 * (gray_alb / (lid_normalized + epsilon)) - b2)

                    # Apply mask
                    gray_alb_diff_masked = gray_alb_diff * nonzero_mask * 10
                    gray_shd_diff_masked = gray_shd_diff * nonzero_mask

                    # Combine the masked differences to form the loss_lid
                    val_loss_lid = (gray_alb_diff_masked + gray_shd_diff_masked).sum()

                    val_loss_lid_scaled = loss_lid_coeff * val_loss_lid / max(1e-8, avg_loss_lid)
                    val_loss += val_loss_lid_scaled
                    val_running_loss_lid += val_loss_lid_scaled

                if include_loss_alb_smoothness:
                    val_loss_alb = alb_smoothness_loss(pred_alb, 10,None,False, include_chroma_weights, luminance, chromaticity)
                    val_loss_alb_scaled = loss_alb_smoothness_coeff * val_loss_alb / max(1e-8, avg_loss_alb)
                    val_loss += val_loss_alb_scaled
                    val_running_loss_alb += val_loss_alb_scaled

                if include_loss_shading:
                    val_loss_shd = shading_loss(pred_shd, img, 10)
                    val_loss_shd_scaled = loss_shading_coeff * val_loss_shd / max(1e-8, avg_loss_shd)
                    val_running_loss_shd += val_loss_shd_scaled.item()
                    val_loss += val_loss_shd_scaled
                # Accumulate the validation loss
                val_running_loss += val_loss

        # Calculate average validation loss for the epoch
        epoch_val_loss = val_running_loss / len(val_loader)
        epoch_val_loss_alb = val_running_loss_alb / len(val_loader)
        epoch_val_loss_lid = val_running_loss_lid / len(val_loader)
        epoch_val_loss_shd = val_running_loss_shd / len(val_loader)

        if epoch_val_loss < best_val_loss - early_stopping_delta and epoch > 1:
            best_val_loss = epoch_val_loss
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
        print(epochs_without_improvement)
        if epochs_without_improvement >= early_stopping_patience:
            print(f"Early stopping triggered at epoch {epoch+1}")
            break

        # Log validation loss to wandb
        if wandb_activation:
            wandb.log({"epoch_val_loss": epoch_val_loss, "epoch": epoch})
            wandb.log({"epoch_val_loss_alb": epoch_val_loss_alb, "epoch": epoch})
            wandb.log({"epoch_val_loss_lid": epoch_val_loss_lid, "epoch": epoch})
            wandb.log({"epoch_val_loss_shd": epoch_val_loss_shd, "epoch": epoch})

        print(f"Epoch {epoch+1}/{epochs}, Validation Loss: {epoch_val_loss}")
    end_time = time.time()
    print(f"The training lasted : {end_time}")

def setup_network(model_path):
    """
    Set up and return the PointNet network.

    Returns:
    torch.nn.Module: The initialized PointNet network.
    """
    PoIntNet = PoInt_Net(k=3)
    network = PoIntNet.cuda()

    # Load the entire checkpoint
    checkpoint = torch.load(model_path)

    # Extract only the model's state_dict
    if 'model_state_dict' in checkpoint:
        network.load_state_dict(checkpoint['model_state_dict'])
    else:
        # If the model_state_dict key is not present, assume the entire file is the state_dict
        network.load_state_dict(checkpoint)

    return network

def main_train():
    """
    Main training function. Parses command-line arguments, initializes the model,
    dataloader, loss function, and optimizer, then starts the training process.
    """
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=30, help='number of epochs to train for')
    parser.add_argument('--batch_size', type=int, default=4, help='input batch size')
    parser.add_argument('--lr', type=float, default=0.1, help='learning rate')

    parser.add_argument('--workers', type=int, default=0, help='number of data loading workers')
    parser.add_argument('--gpu_ids', type=str, default='0', help='choose GPU')
    parser.add_argument('--wandb', type=bool, default=False)

    parser.add_argument('--path_to_train_pc', type=str, default='./Data/pcd/pcd_split_0.5_train/', help='path to train data')
    parser.add_argument('--path_to_train_nm', type=str, default='./Data/gts/nm_split_0.5_train/', help='path to train data')
    parser.add_argument('--path_to_val_pc', type=str, default='./Data/pcd/pcd_split_0.5_val/', help='path to val data')
    parser.add_argument('--path_to_val_nm', type=str, default='./Data/gts/nm_split_0.5_val/', help='path to val data')

    parser.add_argument('--save_model_path', type=str, default='./pre_trained_model/shd_{lr:.4f}_{loss_shading_coeff:4f}.pth', help='path to save the trained model')
    parser.add_argument('--path_to_model', type=str, default='./pre_trained_model/all_intrinsic.pth', help='path to the pre-trained model')

    parser.add_argument('--include_loss_recon', type=bool, default=True, help='whether to include reconstruction loss in the total loss computation')
    parser.add_argument('--include_loss_alb_smoothness', type=bool, default=False, help='whether to include albedo smoothness loss in the total loss computation')
    parser.add_argument('--include_loss_lid', type=bool, default=False, help='whether to include loss_lid in the total loss computation')
    parser.add_argument('--include_loss_shading', type=bool, default=False, help='whether to include shading loss in the total loss computation')
    
    parser.add_argument('--loss_recon_coeff', type=float, default=1.0, help='coefficient to scale the impact of recon_lid')
    parser.add_argument('--loss_lid_coeff', type=float, default=1.0, help='coefficient to scale the impact of loss_lid')
    parser.add_argument('--loss_alb_smoothness_coeff', type=float, default=1.0, help='coefficient to scale the impact of albedo smoothness loss')
    parser.add_argument('--loss_shading_coeff', type=float, default=1.0, help='coefficient to scale the impact of shading loss')
    parser.add_argument('--include_chroma_weights', type=bool, default=False, help='whether to include chroma weights in the total loss computation')
    
    opt = parser.parse_args()

    extract_substring = lambda fp: fp[fp.rfind("/", 0, fp.rfind("/")) + 1:] if fp.count('/') >= 2 else ""

    # Get current date and time
    current_datetime = datetime.datetime.now()
    date_time_str = current_datetime.strftime("%Y%m%d_%H%M%S")

    if opt.include_loss_alb_smoothness == True and opt.include_loss_shading == False:
        opt.save_model_path = f'./pre_trained_model/albedo_only/{opt.loss_alb_smoothness_coeff:4f}_{opt.lr:.4f}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)

    if opt.include_loss_alb_smoothness == False and opt.include_loss_shading == True:
        opt.save_model_path = f'./pre_trained_model/shading_only/{opt.loss_shading_coeff:4f}_{opt.lr:.4f}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)

    if opt.include_loss_alb_smoothness == True and opt.include_chroma_weights == True:
        opt.save_model_path = f'./pre_trained_model/albedo_chroma/{opt.loss_alb_smoothness_coeff:4f}_{opt.lr:.4f}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)
    
    if opt.include_loss_lid == True and opt.include_loss_alb_smoothness == False and opt.include_loss_shading == False:
        opt.save_model_path = f'./pre_trained_model/lid_only/{opt.loss_lid_coeff:4f}_{opt.lr:.4f}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)
        
    if opt.include_loss_alb_smoothness == False and opt.include_loss_shading == False and opt.include_loss_lid == False and opt.include_loss_recon == True:
        opt.save_model_path = f'./pre_trained_model/recon_only/{opt.lr:.4f}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)
    
    if opt.include_loss_alb_smoothness == True and opt.include_loss_shading == True and opt.include_loss_lid == True:
        opt.save_model_path = f'./pre_trained_model/all_losses/{opt.loss_alb_smoothness_coeff:4f}_{opt.loss_shading_coeff:4f}_{opt.loss_lid_coeff:4f}_{opt.lr}_{opt.batch_size}_{date_time_str}.pth'
        wandb_name = extract_substring(opt.save_model_path)
        
    if opt.wandb:
        wandb.init(project="iid_pc", name =wandb_name ,config={
        "epochs": opt.epochs,
        "batch_size": opt.batch_size,
        "learning_rate": opt.lr,
        "loss_lid_coeff" : opt.loss_lid_coeff,
        "s1_init": 1.0,
        "s2_init": 1.0,
        "b1_init": 0.0,
        "b2_init": 0.0,
    })
        config = wandb.config

    # Set the GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = opt.gpu_ids

    # Load dataset and create DataLoader
    train_set = PcdIID_Recon(opt.path_to_train_pc, opt.path_to_train_nm, 
                           train=True)
    val_set = PcdIID_Recon(opt.path_to_val_pc, opt.path_to_val_nm, 
                           train=False)
    train_loader = torch.utils.data.DataLoader(train_set, batch_size=opt.batch_size, 
                                             shuffle=False, num_workers=opt.workers, 
                                             collate_fn=custom_collate_fn, drop_last=True)
    
    val_loader = torch.utils.data.DataLoader(val_set, batch_size=opt.batch_size, 
                                             shuffle=False, num_workers=opt.workers, 
                                             collate_fn=custom_collate_fn, drop_last=True)
    
    print(f"There is {len(train_loader)} batches in the trainloader")

    # Initialize the network
    print(f"The pre-trained model is loaded from {opt.path_to_model}")
    network = setup_network(opt.path_to_model)

    # Parameters for LiDAR Loss
    s1 = nn.Parameter(torch.tensor([1.0], device='cuda'))
    s2 = nn.Parameter(torch.tensor([1.0], device='cuda'))
    b1 = nn.Parameter(torch.tensor([0.0], device='cuda'))
    b2 = nn.Parameter(torch.tensor([0.0], device='cuda'))

    # Define the loss function and optimizer
    criterion = nn.MSELoss(reduction = 'sum')
    optimizer = optim.SGD([
    {'params': network.parameters()},
    {'params': [s1, s2, b1, b2]}
    ], lr=opt.lr, momentum=0.9)   # 0.9 is a common value for momentum, but you can adjust it as needed


    # Start the training    
    train_model(network, train_loader, val_loader, optimizer, criterion, opt.epochs, s1, s2, b1, b2, opt.save_model_path, opt.include_loss_recon, opt.include_loss_lid, opt.include_loss_alb_smoothness, opt.include_loss_shading, opt.include_chroma_weights, opt.loss_recon_coeff, opt.loss_lid_coeff, opt.loss_alb_smoothness_coeff, opt.loss_shading_coeff, opt.wandb)

    # Save the trained model
    torch.save({
        'model_state_dict': network.state_dict(),
        's1': s1.item(),
        's2': s2.item(),
        'b1': b1.item(),
        'b2': b2.item()
    }, opt.save_model_path)

    print("The model was saved at {}".format(opt.save_model_path))

    if opt.wandb:
        wandb.save(opt.save_model_path)
        wandb.finish()

if __name__ == "__main__":
    main_train()
