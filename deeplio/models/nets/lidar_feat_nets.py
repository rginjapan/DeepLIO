import torch
from torch import nn
from torch.nn import functional as F

from .base_net import BaseNet, num_flat_features
from .pointseg_modules import Fire, SELayer
from .pointseg_net import PSEncoder, PSDecoder
from ..misc import get_config_container


class BaseLidarFeatNet(BaseNet):
    def __init__(self, input_shape, cfg):
        super(BaseLidarFeatNet, self).__init__()
        self.p = cfg['dropout']
        self.fusion = cfg['fusion']
        self.cfg_container = get_config_container()
        self.seq_size = self.cfg_container.seq_size
        self.timestamps = self.cfg_container.timestamps
        self.combinations = self.cfg_container.combinations
        self.input_shape = input_shape
        self.output_shape = None

    def calc_output_shape(self):
        c, h, w = self.input_shape
        input1 = torch.rand((1, self.timestamps, c, h, w))
        input2 = torch.rand((1, self.timestamps, c, h, w))
        self.eval()
        with torch.no_grad():
            out = self.forward([input1, input2])
        return out.shape

    def get_output_shape(self):
        return self.output_shape


class LidarPointSegFeat(BaseLidarFeatNet):
    def __init__(self, input_shape, cfg, bn_d=0.1):
        super(LidarPointSegFeat, self).__init__(input_shape, cfg)
        self.part = cfg['part'].lower()
        self.bn_d = bn_d

        c, h, w = self.input_shape

        self.encoder1 = PSEncoder((2*c, h, w), cfg)
        self.encoder2 = PSEncoder((2*c, h, w), cfg)

        # shapes of  x_1a, x_1b, x_se1, x_se2, x_se3, x_el
        enc_out_shapes = self.encoder1.get_output_shape()

        # number of output channels in encoder
        b, c, h, w = enc_out_shapes[4]

        alpha = 2 if self.fusion == 'cat' else 1
        self.fire12 = nn.Sequential(Fire(alpha*c, 64, 256, 256, bn=True, bn_d=self.bn_d),
                                    Fire(512, 64, 256, 256, bn=True, bn_d=self.bn_d),
                                    SELayer(512, reduction=2),
                                    nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1)))

        self.fire34 = nn.Sequential(Fire(512, 80, 384, 384, bn=True, bn_d=self.bn_d),
                                    Fire(768, 80, 384, 384, bn=True, bn_d=self.bn_d),
                                    #nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1)),
                                    nn.AdaptiveAvgPool2d((1, 1)))

        if self.p > 0.:
            self.drop = nn.Dropout(self.p)

        self.output_shape = self.calc_output_shape()

    def forward(self, x):
        """

        :param inputs: images of dimension [BxTxCxHxW], where T is seq-size+1, e.g. 2+1
        :return: outputs: features of dim [BxTxN]
        mask0: predicted mask to each time sequence
        """
        imgs_xyz, imgs_normals = x[0], x[1]
        b, t, c, h, w = imgs_xyz.shape
        imgs_xyz = imgs_xyz.reshape(b, t * c, h, w)
        imgs_normals = imgs_xyz.reshape(b, t * c, h, w)

        x_1a_0, x_1b_0, x_se1_0, x_se2_0, x_se3_0, x_el_0 = self.encoder1(imgs_xyz)
        x_feat_0 = x_se3_0

        x_1a_1, x_1b_1, x_se1_1, x_se2_1, x_se3_1, x_el_1 = self.encoder2(imgs_normals)
        x_feat_1 = x_se3_1

        if self.fusion == 'cat':
            x = torch.cat((x_feat_0, x_feat_1), dim=1)
        elif self.fusion == 'add':
            x = x_feat_0 + x_feat_1
        else:
            x = x_feat_0 - x_feat_1

        x = self.fire12(x)
        x = self.fire34(x)

        if self.p > 0.:
            x = self.drop(x)

        # reshape output to BxTxCxHxW
        x = x.view(b, num_flat_features(x, 1))
        return x


class LidarSimpleFeat0(BaseLidarFeatNet):
    def __init__(self, input_shape, cfg):
        super(LidarSimpleFeat0, self).__init__(input_shape, cfg)
        c, h, w = self.input_shape

        self.encoder1 = FeatureNetSimple0([2*c, h, w])
        self.encoder2 = FeatureNetSimple0([2*c, h, w])

        if self.p > 0:
            self.drop = nn.Dropout(self.p)

        self.output_shape = self.calc_output_shape()

    def forward(self, x):
        """
        :param inputs: images of dimension [BxTxCxHxW], where T is seq-size+1, e.g. 2+1
        :return: outputs: features of dim [BxTxN]
        mask0: predicted mask to each time sequence
        """
        imgs_xyz, imgs_normals = x[0], x[1]

        b, t, c, h, w = imgs_xyz.shape
        imgs_xyz = imgs_xyz.reshape(b, t * c, h, w)
        imgs_normals = imgs_xyz.reshape(b, t * c, h, w)

        x_feat_0 = self.encoder1(imgs_xyz)
        x_feat_1 = self.encoder2(imgs_normals)

        if self.fusion == 'cat':
            x = torch.cat((x_feat_0, x_feat_1), dim=1)
        elif self.fusion == 'add':
            x = x_feat_0 + x_feat_1
        else:
            x = x_feat_0 - x_feat_1

        if self.p > 0.:
            x = self.drop(x)

        # reshape output to BxTxCxHxW
        x = x.view(b, num_flat_features(x, 1))
        return x


class LidarSimpleFeat1(BaseLidarFeatNet):
    def __init__(self, input_shape, cfg):
        super(LidarSimpleFeat1, self).__init__(input_shape, cfg)
        bypass = cfg['bypass']
        c, h, w = self.input_shape

        self.encoder1 = FeatureNetSimple1([2*c, h, w], bypass=bypass)
        self.encoder2 = FeatureNetSimple1([2*c, h, w], bypass=bypass)

        if self.p > 0:
            self.drop = nn.Dropout(self.p)

        self.output_shape = self.calc_output_shape()

    def forward(self, x):
        """
        :param inputs: images of dimension [BxSxTxCxHxW], S:=Seq-length T:=#timestamps, e.g. 2+1
        :return: outputs: features of dim [BxTxN]
        mask0: predicted mask to each time sequence
        """
        imgs_xyz, imgs_normals = x[0], x[1]

        b, t, c, h, w = imgs_xyz.shape
        imgs_xyz = imgs_xyz.reshape(b, t * c, h, w)
        imgs_normals = imgs_xyz.reshape(b, t * c, h, w)

        x_feat_0 = self.encoder1(imgs_xyz)
        x_feat_1 = self.encoder2(imgs_normals)

        if self.fusion == 'cat':
            y = torch.cat((x_feat_0, x_feat_1), dim=1)
        elif self.fusion == 'add':
            y = x_feat_0 + x_feat_1
        else:
            y = x_feat_0 - x_feat_1

        if self.p > 0.:
            y = self.drop(y)

        # reshape output to BxTxCxHxW
        y = y.view(b, num_flat_features(y, 1))
        return y


class FeatureNetSimple0(nn.Module):
    """Simple Conv. based Feature Network
    """
    def __init__(self, input_shape):
        super(FeatureNetSimple0, self).__init__()
        self.input_shape = input_shape
        c, h, w = self.input_shape

        self.conv1 = nn.Conv2d(c, out_channels=32, kernel_size=(3, 7), stride=(1, 2), padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.pool1 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv2 = nn.Conv2d(32, out_channels=32, kernel_size=(3, 5), stride=(1, 1), padding=1)
        self.bn2 = nn.BatchNorm2d(32)
        self.pool2 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv3 = nn.Conv2d(32, out_channels=64, kernel_size=3, stride=(1, 1), padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        self.pool3 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv4 = nn.Conv2d(64, out_channels=64, kernel_size=3, stride=(1, 1), padding=1)
        self.bn4 = nn.BatchNorm2d(64)
        self.pool4 = nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1), ceil_mode=True)

        self.conv5 = nn.Conv2d(64, out_channels=64, kernel_size=3, stride=(1, 1), padding=1)
        self.bn5 = nn.BatchNorm2d(64)
        self.pool5 = nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1), ceil_mode=True)

    def forward(self, x):
        out = F.relu(self.conv1(x))
        out = self.bn1(out)
        out = self.pool1(out)

        out = F.relu(self.conv2(out))
        out = self.bn2(out)
        out = self.pool2(out)

        out = F.relu(self.conv3(out))
        out = self.bn3(out)
        out = self.pool3(out)

        out = F.relu(self.conv4(out))
        out = self.bn4(out)
        out = self.pool4(out)

        out = F.relu(self.conv5(out))
        out = self.bn5(out)
        out = self.pool5(out)
        return out


class FeatureNetSimple1(nn.Module):
    """Simple Conv. based Feature Network with optinal bypass connections"""
    def __init__(self, input_shape, bypass=False):
        super(FeatureNetSimple1, self).__init__()

        self.bypass = bypass
        self.input_shape = input_shape
        c, h, w = self.input_shape

        self.conv1 = nn.Conv2d(c, out_channels=32, kernel_size=(5, 7), stride=(1, 2), padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.pool1 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv2 = nn.Conv2d(32, out_channels=32, kernel_size=(3, 5), stride=(1, 1), padding=1)
        self.bn2 = nn.BatchNorm2d(32)
        self.pool2 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv3 = nn.Conv2d(32, out_channels=64, kernel_size=3, stride=(1, 1), padding=1)
        self.bn3 = nn.BatchNorm2d(64)

        self.conv4 = nn.Conv2d(64, out_channels=64, kernel_size=3, stride=(1, 1), padding=1)
        self.bn4 = nn.BatchNorm2d(64)
        self.pool4 = nn.MaxPool2d(kernel_size=3, stride=(1, 2), padding=(1, 1), ceil_mode=True)

        self.conv5 = nn.Conv2d(64, out_channels=128, kernel_size=3, stride=(1, 1), padding=1)
        self.bn5 = nn.BatchNorm2d(128)

        self.conv6 = nn.Conv2d(128, out_channels=128, kernel_size=3, stride=(1, 1), padding=1)
        self.bn6 = nn.BatchNorm2d(128)
        self.pool6 = nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1), ceil_mode=True)

        self.conv7 = nn.Conv2d(128, out_channels=256, kernel_size=3, stride=(1, 1), padding=1)
        self.bn7 = nn.BatchNorm2d(256)
        # self.pool7 = nn.MaxPool2d(kernel_size=3, stride=(2, 2), padding=(1, 1), ceil_mode=True)
        self.pool7 = nn.AdaptiveAvgPool2d((1, 1))

    def forward(self, x):
        # 1. block
        out = F.relu(self.conv1(x), inplace=True)
        out = self.bn1(out)
        out = self.pool1(out)

        # 2. block
        out = F.relu(self.conv2(out), inplace=True)
        out = self.bn2(out)
        out = self.pool2(out)

        # 3. block
        out = F.relu(self.conv3(out), inplace=True)
        out = self.bn3(out)
        identitiy = out

        out = F.relu(self.conv4(out), inplace=True)
        out = self.bn4(out)
        if self.bypass:
            out += identitiy
        out = self.pool4(out)

        # 4. block
        out = F.relu(self.conv5(out), inplace=True)
        out = self.bn5(out)
        identitiy = out

        out = F.relu(self.conv6(out), inplace=True)
        out = self.bn6(out)
        if self.bypass:
            out += identitiy
        out = self.pool6(out)

        out = F.relu(self.conv7(out), inplace=True)
        out = self.bn7(out)
        out = self.pool7(out)
        return out

