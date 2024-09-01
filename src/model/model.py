import torch
from torch import nn

from .blocks.encoder_block import build_encoder
from .blocks.decoder_block import build_decoder
from .blocks.backbone import build_backbone_customized
from .blocks.mini_detector import MiniDetector
from ..utils.misc import nested_tensor_from_tensor_list, inverse_sigmoid
from ..utils.positional_embedding import gen_sineembed_for_position


class ObjDetSplitTransformer(nn.Module):
    def __init__(
        self,
        args,
        backbone: nn.Module,
        encoder: nn.Module,
        decoder: nn.Module,
    ):
        super(ObjDetSplitTransformer, self).__init__()

        # we use resnet50 as feature extractor
        self._backbone = backbone
        self._encoder = encoder
        self._decoder = decoder

        self._hidden_dim = args.hidden_dim
        self._cls_embed = (
            nn.Linear(in_features=self._hidden_dim, out_features=args.num_cls),
        )
        self._bbox_embed = nn.Sequential(
            nn.Linear(in_features=self._hidden_dim, out_features=self._hidden_dim),
            nn.ReLU(),
            nn.Linear(in_features=self._hidden_dim, out_features=4),
        )
        self._reg_ffn = nn.Sequential(
            nn.Linear(in_features=self._hidden_dim, out_features=self._hidden_dim),
            nn.ReLU(),
            nn.Linear(in_features=self._hidden_dim, out_features=self._hidden_dim),
            nn.ReLU(),
            nn.Linear(in_features=self._hidden_dim, out_features=2),
        )
        self._pos_scale = nn.Sequential(
            nn.Linear(in_features=self._hidden_dim, out_features=self._hidden_dim),
            nn.ReLU(),
            nn.Linear(in_features=self._hidden_dim, out_features=2),
        )

        # the output channels from ResNet50 is 2048
        self._reduce_dim = nn.Conv2d(
            in_channels=2048,
            out_channels=self._hidden_dim,
            kernel_size=(1, 1),
            stride=1,
        )
        self._mini_detector = MiniDetector(
            top_k=args.top_k,
            reg_ffn=self._reg_ffn,
            class_embed=self._class_embed,
            bbox_embed=self._bbox_embed,
        )

    def forward(self, inputs):
        if isinstance(inputs, (list, torch.Tensor)):
            inputs = nested_tensor_from_tensor_list(inputs)

        features, pos = self._backbone(inputs)

        x, mask = features[-1].decompose()
        batch_size, channels, height, width = x.shape

        x = self._reduce_dim(x)

        x = self._encoder(x, mask, pos[-1])
        encoder_output = x

        # shape of pos -> (batch_size, channels, height, width)
        #              -> (height*width, batch_size, channels)
        fine_pos = pos[-1].flatten(2).permute(2, 0, 1)
        fine_pos = fine_pos * self._encoder._pos_scale(
            x.flatten(2).permute(2, 0, 1).contiguous()
        )
        fine_pos = (
            fine_pos.view(height, width, batch_size, -1)
            .permute(2, 3, 0, 1)
            .contiguous()
        )
        fine_mask = mask

        selected_objects, selected_objects_center, det_output = self._mini_detector(
            x, fine_pos
        )

        selected_objects_pos_embed = gen_sineembed_for_position(
            selected_objects_center, self._hidden_dim
        )
        selected_centers = selected_objects_center.transpose(0, 1)

        x = selected_objects

        x, center_offset = self._decoder(
            selected_objects,
            encoder_output,
            fine_mask,
            selected_objects_pos_embed,
            selected_centers,
            self._mini_detector._bbox_embed,
        )

        cls_x, reg_x = torch.split(x, [self._hidden_dim, self._hidden_dim], dim=-1)
        center_offset_before_sigmoid = inverse_sigmoid(center_offset)

        cls_output = self._cls_embed(cls_x)

        tmp = self._bbox_embed(reg_x)
        tmp[..., :2] += center_offset_before_sigmoid
        bbox_output = tmp.sigmoid()

        return cls_output, bbox_output, det_output


def build_model(args):
    position_index_1d = torch.arange(start=0, end=7)
    pos_idx1 = position_index_1d.unsqueeze(0).broadcast_to(7, 7)
    pos_idx2 = position_index_1d.unsqueeze(1).broadcast_to(7, 7)
    position_index_2d = torch.stack([pos_idx2, pos_idx1], dim=-1)

    encoder = build_encoder(args=args, position_index_2d=position_index_2d)
    # decoder = build_decoder(args=args, position_index_2d=position_index_2d)
    backbone = build_backbone_customized(args=args)

    decoder = nn.Linear(10, 10)

    model = ObjDetSplitTransformer(
        args=args, backbone=backbone, encoder=encoder, decoder=decoder
    )

    return model
