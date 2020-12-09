import math
from scipy.stats import entropy
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F 

from histocartography.utils.io import get_device


class ExplainerModel(nn.Module):
    def __init__(
        self,
        model,
        adj,
        x,
        init_probs,
        model_params,
        train_params,
        cuda=False,
        use_sigmoid=True,
    ):

        super(ExplainerModel, self).__init__()

        # set data & model
        self.adj = adj
        self.x = x
        self.model = model
        if cuda:
            self.model = self.model.cuda()
        self.init_probs = init_probs
        self.label = torch.argmax(init_probs, dim=1)

        # set model parameters
        self.cuda = cuda
        self.device = get_device(self.cuda)
        self.mask_act = model_params['mask_activation']
        init_strategy = model_params['init']
        self.mask_bias = None
        self.use_sigmoid = use_sigmoid

        # build learnable parameters: edge mask & feat mask (& node_mask)
        num_nodes = adj.size()[1]
        self.num_nodes = num_nodes
        self.mask, _ = self._build_edge_mask(num_nodes, init_strategy=init_strategy)
        self.node_mask = self._build_node_mask(num_nodes, init_strategy='const')
        self.diag_mask = torch.ones(num_nodes, num_nodes) - torch.eye(num_nodes)

        # group them
        params = [self.mask, self.node_mask]
        if self.mask_bias is not None:
            params.append(self.mask_bias)

        if self.cuda:
            self.diag_mask = self.diag_mask.cuda()

        # build optimizer
        self._build_optimizer(params, train_params)

        self.coeffs = model_params['loss']

    def _build_optimizer(self, params, train_params):
        self.optimizer = optim.Adam(params, lr=train_params['lr'], weight_decay=train_params['weight_decay'])

    def _build_edge_mask(self, num_nodes, init_strategy="normal", const_val=1.0):
        mask = nn.Parameter(torch.FloatTensor(num_nodes, num_nodes))
        if init_strategy == "normal":
            std = nn.init.calculate_gain("relu") * math.sqrt(
                2.0 / (num_nodes + num_nodes)
            )
            with torch.no_grad():
                mask.normal_(1.0, std)
        elif init_strategy == "const":
            nn.init.constant_(mask, const_val)

        if self.mask_bias is not None:
            mask_bias = nn.Parameter(torch.FloatTensor(num_nodes, num_nodes))
            nn.init.constant_(mask_bias, 0.0)
        else:
            mask_bias = None

        return mask, mask_bias

    def _build_node_mask(self, num_nodes, init_strategy="normal", const_val=1.0):
        node_mask = nn.Parameter(torch.FloatTensor(num_nodes))
        if init_strategy == "normal":
            std = nn.init.calculate_gain("relu") * math.sqrt(
                2.0 / (num_nodes + num_nodes)
            )
            with torch.no_grad():
                node_mask.normal_(1.0, std)
        elif init_strategy == "const":
            nn.init.constant_(node_mask, const_val)
        return node_mask

    def _get_adj_mask(self, with_zeroing=False):
        if self.mask_act == "sigmoid":
            sym_mask = self.sigmoid(self.mask, t=2)
        elif self.mask_act == "relu":
            sym_mask = nn.ReLU()(self.mask)
        else:
            raise ValueError('Unsupported mask activation {}. Options'
                             'are "sigmoid", "ReLU"'.format(self.mask_act))
        sym_mask = (sym_mask + sym_mask.t()) / 2
        if with_zeroing:
            sym_mask = ((self.adj != 0).to(self.device).to(torch.float) * sym_mask)
        return sym_mask        

    def _masked_adj(self):
        sym_mask = self._get_adj_mask()
        adj = self.adj.cuda() if self.cuda else self.adj
        masked_adj = adj * sym_mask
        if self.mask_bias:
            bias = (self.mask_bias + self.mask_bias.t()) / 2
            bias = nn.ReLU6()(bias * 6) / 6
            masked_adj += (bias + bias.t()) / 2
        masked_adj = masked_adj * self.diag_mask
        return masked_adj

    def _get_node_feats_mask(self):
        if self.mask_act == "sigmoid":
            node_mask = self.sigmoid(self.node_mask, t=10)
        elif self.mask_act == "relu":
            node_mask = nn.ReLU()(self.node_mask)
        else:
            raise ValueError('Unsupported mask activation {}. Options'
                             'are "sigmoid", "ReLU"'.format(self.mask_act))
        return node_mask

    @staticmethod
    def sigmoid(x, t=1):
        return 1 / (1 + torch.exp(-t * x))

    def _masked_node_feats(self):
        node_mask = self._get_node_feats_mask()
        x = self.x * torch.stack(self.x.shape[-1]*[node_mask], dim=1).unsqueeze(dim=0)
        return x 

    def forward(self):
        masked_x = self._masked_node_feats()
        graph = [self.adj, masked_x]
        ypred = self.model(graph)
        return ypred, masked_x

    def distillation_loss(self, inner_logits):
        log_output = nn.LogSoftmax(dim=1)(inner_logits)
        cross_entropy = self.init_probs * log_output
        return -torch.mean(torch.sum(cross_entropy, dim=1))

    def loss(self, pred):
        """
        Args:
            pred: prediction made by current model
        """

        # 1. cross-entropy + distillation loss
        ce_loss = F.cross_entropy(pred.unsqueeze(dim=0), self.label)
        distillation_loss = self.distillation_loss(pred.unsqueeze(dim=0))
        alpha = torch.FloatTensor([entropy(torch.nn.Softmax()(pred).cpu().detach().numpy())]) /\
                torch.log(torch.FloatTensor([self.init_probs.shape[1]]))
        alpha = alpha.to(self.device)
        pred_loss = self.coeffs['ce'] * (alpha * ce_loss + (1 - alpha) * distillation_loss)

        # 2. node loss 
        node_mask = self._get_node_feats_mask()
        node_loss = self.coeffs["node"] * torch.sum(node_mask)

        # 3. node entropy loss
        node_ent = -node_mask * torch.log(node_mask) - (1 - node_mask) * torch.log(1 - node_mask)
        node_ent_loss = self.coeffs["node_ent"] * torch.mean(node_ent)

        # 4. sum all the losses
        loss = pred_loss + node_loss + node_ent_loss

        return loss
