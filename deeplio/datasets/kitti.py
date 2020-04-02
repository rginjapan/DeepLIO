import os
import torch
import glob
import yaml
import datetime as dt
import random
from threading import Thread

import time # for start stop calc

import numpy as np

import torch.utils.data as data

from deeplio.common import utils
from deeplio.common.laserscan import LaserScan
from deeplio.common.logger import PyLogger


class KittiRawData:
    """ KiitiRawData
    more or less same as pykitti with some application specific changes
    """
    MAX_DIST_HDL64 = 120.
    IMU_LENGTH = 10.25

    def __init__(self, base_path, date, drive, cfg, **kwargs):
        self.drive = drive
        self.date = date
        self.calib_path = os.path.join(base_path, date)
        self.data_path = os.path.join(base_path, date, drive)
        self.frames = kwargs.get('frames', None)

        self.image_width = cfg['image-width']
        self.image_height = cfg['image-height']
        self.fov_up = cfg['fov-up']
        self.fov_down = cfg['fov-down']
        self.seq_size = cfg['sequence-size']

        # Find all the data files
        self._get_file_lists()

        # Pre-load data that isn't returned as a generator
        # Pre-load data that isn't returned as a generator
        #self._load_calib()
        self._load_timestamps()
        #self._load_oxts()

        self.imu_get_counter = 0

    def __len__(self):
        return len(self.velo_files)

    def get_velo(self, idx):
        """Read velodyne [x,y,z,reflectance] scan at the specified index."""
        return utils.load_velo_scan(self.velo_files[idx])

    def get_velo_image(self, idx):
        scan = LaserScan(H=self.image_height, W=self.image_width, fov_up=self.fov_up, fov_down=self.fov_down)
        scan.open_scan(self.velo_files[idx])
        scan.do_range_projection()
        # collect projected data and adapt ranges

        proj_xyz = scan.proj_xyz
        proj_remission = scan.proj_remission
        proj_range = scan.proj_range
        proj_range_xy = scan.proj_range_xy

        image = np.dstack((proj_xyz, proj_remission, proj_range, proj_range_xy))
        return image

    def _get_file_lists(self):
        """Find and list data files for each sensor."""
        self.oxts_files = sorted(glob.glob(
            os.path.join(self.data_path, 'oxts', 'data', '*.txt')))
        self.velo_files = sorted(glob.glob(
            os.path.join(self.data_path, 'velodyne_points',
                         'data', '*.*')))

        # Subselect the chosen range of frames, if any
        if self.frames is not None:
            self.oxts_files = utils.subselect_files(
                self.oxts_files, self.frames)
            self.velo_files = utils.subselect_files(
                self.velo_files, self.frames)

        self.oxts_files = np.asarray(self.oxts_files)
        self.velo_files = np.asarray(self.velo_files)

    def _load_calib_rigid(self, filename):
        """Read a rigid transform calibration file as a numpy.array."""
        filepath = os.path.join(self.calib_path, filename)
        data = utils.read_calib_file(filepath)
        return utils.transform_from_rot_trans(data['R'], data['T'])

    def _load_calib(self):
        """Load and compute intrinsic and extrinsic calibration parameters."""
        # We'll build the calibration parameters as a dictionary, then
        # convert it to a namedtuple to prevent it from being modified later
        data = {}

        # Load the rigid transformation from IMU to velodyne
        data['T_velo_imu'] = self._load_calib_rigid('calib_imu_to_velo.txt')

    def _load_timestamps(self):
        """Load timestamps from file."""
        timestamp_file_imu = os.path.join(self.data_path, 'oxts', 'timestamps.txt')
        timestamp_file_velo = os.path.join(self.data_path, 'velodyne_points', 'timestamps.txt')

        # Read and parse the timestamps
        self.timestamps_imu = []
        with open(timestamp_file_imu, 'r') as f:
            for line in f.readlines():
                # NB: datetime only supports microseconds, but KITTI timestamps
                # give nanoseconds, so need to truncate last 4 characters to
                # get rid of \n (counts as 1) and extra 3 digits
                t = dt.datetime.strptime(line[:-4], '%Y-%m-%d %H:%M:%S.%f')
                self.timestamps_imu.append(t)
        self.timestamps_imu = np.array(self.timestamps_imu)

        # Read and parse the timestamps
        self.timestamps_velo = []
        with open(timestamp_file_velo, 'r') as f:
            for line in f.readlines():
                # NB: datetime only supports microseconds, but KITTI timestamps
                # give nanoseconds, so need to truncate last 4 characters to
                # get rid of \n (counts as 1) and extra 3 digits
                t = dt.datetime.strptime(line[:-4], '%Y-%m-%d %H:%M:%S.%f')
                self.timestamps_velo.append(t)
        self.timestamps_velo = np.array(self.timestamps_velo)

    def _load_oxts(self):
        """Load OXTS data from file."""
        self.oxts = np.array(utils.load_oxts_packets_and_poses(self.oxts_files))

    def _load_oxts_lazy(self, indices):
        oxts = utils.load_oxts_packets_and_poses(self.oxts_files[indices])
        return oxts

    def calc_gt_from_oxts(self, oxts):
        transformations = [oxt.T_w_imu for oxt in oxts]

        T_w0 = transformations[0]
        R_w0 = T_w0[:3, :3]
        t_w0 = T_w0[:3, 3]
        T_w0_inv = np.identity(4)
        T_w0_inv[:3, :3] = R_w0.T
        T_w0_inv[:3, 3] = -np.matmul(R_w0.T, t_w0)

        gt_s = [np.matmul(T_w0_inv, T_0i) for T_0i in transformations]
        return gt_s


class Kitti(data.Dataset):
    def __init__(self, config, ds_type='train', transform=None):
        """
        :param root_path:
        :param config: Configuration file including split settings
        :param transform:
        """
        ds_config = config['datasets']['kitti']
        root_path = ds_config['root-path']

        self.transform = transform

        self.ds_type = ds_type
        self.seq_size = ds_config['sequence-size']
        self.channels = config['channels']

        self.dataset = []
        self.length_each_drive = []
        self.bins = []
        self.images = [None] * self.seq_size

        # Since we are intrested in sequence of lidar frame - e.g. multiple frame at each iteration,
        # depending on the sequence size and the current wanted index coming from pytorch dataloader
        # we must switch between each drive if not enough frames exists in that specific drive wanted from dataloader,
        # therefor we separate valid indices in each drive in bins.
        last_bin_end = -1
        for date, drives in ds_config[self.ds_type].items():
            for drive in drives:
                ds = KittiRawData(root_path, str(date), str(drive), ds_config)

                length = len(ds)

                bin_start = last_bin_end + 1
                bin_end = bin_start + length - 1
                self.bins.append([bin_start, bin_end])
                last_bin_end = bin_end

                self.length_each_drive.append(length)
                self.dataset.append(ds)

        self.bins = np.asarray(self.bins)
        self.length_each_drive = np.array(self.length_each_drive)

        self.length = self.bins.flatten()[-1] + 1

        self.logger = PyLogger(name="KittiDataset")

        # printing dataset informations
        self.logger.info("Kitti-Dataset Informations")
        self.logger.info("DS-Type: {}, Length: {}, Seq.length: {}".format(ds_type, self.length, self.seq_size))
        for i in range(len(self.length_each_drive)):
            date = self.dataset[i].date
            drive = self.dataset[i].drive
            length = self.length_each_drive[i]
            bins = self.bins[i]
            self.logger.info("Date: {}, Drive: {}, length: {}, bins: {}".format(date, drive, length, bins))

    def load_images(self, dataset, indices):
        threads = [None] * self.seq_size

        for i in range(self.seq_size):
            idx = indices[i]
            img_name = self._buil_img_name(dataset, idx)
            
            threads[i] = Thread(target=self.load_image, args=(dataset, indices[i], i))
            threads[i].start()

        for i in range(self.seq_size):
            threads[i].join()

    def load_image(self, dataset, ds_index, img_index):
        img = dataset.get_velo_image(ds_index)
        img = img[:, :, self.channels]
        img_name = self._buil_img_name(dataset, ds_index)
        self.images[img_index] = img

    def _buil_img_name(self, dataset, index):
        img_name = "{}/{}".format(dataset.data_path, index)
        return img_name

    def __len__(self):
        return self.length

    def __getitem__(self, index):
        if torch.is_tensor(index):
            index = index.tolist()

        idx = -1
        num_drive = -1
        for i, bin in enumerate(self.bins):
            bin_start = bin[0]
            bin_end = bin[1]
            if bin_start <= index <= bin_end:
                idx = index - bin_start
                num_drive = i
                break

        if idx < 0 or num_drive < 0:
            print("Error: No bins and no drive number found!")
            return None

        start = time.time()
        dataset = self.dataset[num_drive]

        # get frame indices
        len_ds = len(dataset)
        if idx <= len_ds - self.seq_size:
            indices = list(range(idx, idx + self.seq_size))
        elif (len_ds - self.seq_size) < idx < len_ds:
            indices = list(range(len_ds - self.seq_size, len_ds))
        else:
            self.logger.error("Wrong index ({}) in {}_{}".format(idx, dataset.date, dataset.drive))
            raise Exception("Wrong index ({}) in {}_{}".format(idx, dataset.date, dataset.drive))

        # Get frame timestamps
        velo_timespamps = [dataset.timestamps_velo[idx] for idx in indices]

        # difference combination of a sequence length
        # e.g. for sequence-length = 3, we have following combinations
        # [0, 1], [0, 2], [1, 2]
        combinations = [[x, y] for y in range(self.seq_size) for x in range(y)]
        # we do not want that the network memorizes an specific combination pattern
        # random.shuffle(combinations)

        for combi in combinations:
            idx_0 = combi[0]
            idx_1 = combi[1]

            velo_start_ts = velo_timespamps[idx_0]
            velo_stop_ts = velo_timespamps[idx_1]

            mask = ((dataset.timestamps_imu >= velo_start_ts) & (dataset.timestamps_imu < velo_stop_ts))
            idxs = np.argwhere(mask).flatten()
            if len(idxs) == 0:
                data = {'valid': False}
                return data

        self.load_images(dataset, indices)
        images = self.images

        images_0 = []
        images_1 = []
        imus = []
        gt_s = []
        for combi in combinations:
            idx_0 = combi[0]
            idx_1 = combi[1]

            images_0.append(images[idx_0])
            images_1.append(images[idx_1])

            velo_start_ts = velo_timespamps[idx_0]
            velo_stop_ts = velo_timespamps[idx_1]

            mask = ((dataset.timestamps_imu >= velo_start_ts) & (dataset.timestamps_imu < velo_stop_ts))
            indices = np.argwhere(mask).flatten()

            oxts = dataset._load_oxts_lazy(indices)
            imu_values = [[oxt.packet.ax, oxt.packet.ay, oxt.packet.az, oxt.packet.wx, oxt.packet.wy, oxt.packet.wz] for oxt in oxts]
            imus.append(imu_values)

            gt = dataset.calc_gt_from_oxts(oxts)
            gt_s.append(gt)

            # print("V: {} I:{}\n   {}   {} \n **********".format(velo_start_ts, imu_ts[0], velo_stop_ts, imu_ts[-1]))

        data = {'images': [np.array(images_0), np.array(images_1)], 'imu': imus, 'ground-truth': gt_s,
                'combinations': combinations, 'valid': True}

        if self.transform:
            data = self.transform(data)

        end = time.time()
        #print("idx:{}, Delta-Time: {}".format(index, end - start))
        return data
