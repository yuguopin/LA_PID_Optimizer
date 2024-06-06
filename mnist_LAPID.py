import warnings

warnings.filterwarnings('ignore')

import torch
import torch.nn as nn
import torchvision.datasets as dsets
import torchvision.transforms as transforms
from torch.autograd import Variable
from pid import PIDOptimizer

from utils import Bar, Logger, AverageMeter, accuracy, mkdir_p, savefig
import torch.nn.functional as F
from config import I, D, batch_size, learning_rate, hidden_size, num_classes, num_epochs, input_size
from torchviz import make_dot


save_file = f'./result/PID_params/mnist_PID I={I} D={D} lr={learning_rate}.txt'


if __name__ == '__main__':
    logger = Logger(save_file, title='mnist')
    logger.set_names(['  L__R\t', '  TLoss\t', '  VLoss\t', 'TAcc\t', '  VAcc\t'])

    train_dataset = dsets.MNIST(root='../PIDoptimizer/data', train=True, transform=transforms.ToTensor(), download=True)
    test_dataset = dsets.MNIST(root='../PIDoptimizer/data', train=False, transform=transforms.ToTensor())

    train_loader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=batch_size, shuffle=True)
    test_loader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=batch_size, shuffle=False)


    class Net(nn.Module):
        def __init__(self, input_size, hidden_size, num_classes):
            super(Net, self).__init__()
            self.fc1 = nn.Linear(input_size, hidden_size)
            self.fc2 = nn.Linear(hidden_size, hidden_size)
            self.fc3 = nn.Linear(hidden_size, num_classes)

        def forward(self, x):
            out = self.fc1(x)
            out = F.relu(out)

            out = self.fc2(out)
            out = F.relu(out)

            out = self.fc3(out)

            return out

    # ================== KI KD 系数生成网络 ===================
    num_layers = 3 * 2  # [weight + bias]
    input_dim = num_layers * 2
    hidden_dim = num_layers * 3
    output_dim = num_layers * 2

    class Gen_Net_PID(nn.Module):
        def __init__(self, input_d, hidden_d, output_d):
            super(Gen_Net_PID, self).__init__()

            self.gen_fc1 = nn.Linear(input_d, hidden_d)
            self.gen_fc2 = nn.Linear(hidden_d, output_d)

        def forward(self, x):
            x = self.gen_fc1(x)
            x = nn.ReLU(inplace=True)(x)
            x = self.gen_fc2(x)
            x = nn.ReLU(inplace=True)(x)

            return x

    # KIKD_Gen_Net = nn.Sequential(
    #     nn.Linear(input_dim, hidden_dim), nn.ReLU(inplace=True),
    #     nn.Linear(hidden_dim, output_dim), nn.ReLU(inplace=True)
    # ).to('cuda')
    KIKD_Gen_Net = Gen_Net_PID(input_dim, hidden_dim, output_dim).cuda()

    # Gen_net_optimizer = torch.optim.SGD(KIKD_Gen_Net.parameters(), lr=0.1)

    # ==========================================================

    net = Net(input_size, hidden_size, num_classes)

    net.cuda()
    net.train()

    criterion = nn.CrossEntropyLoss()

    # 获取模型参数名称与层名称的对应关系
    # param_names_to_layers = {param_name: module_name for param_name, module_name in net.named_parameters()}

    optimizer = PIDOptimizer([{'params': net.parameters()},
                                    {'params': KIKD_Gen_Net.parameters()}],
                             lr=learning_rate, weight_decay=0.0001, momentum=0.9, I=I, D=D)

    optimizer.InitWeightsWith_Lr(net)

    generated_I_params = {}
    generated_D_params = {}

    for epoch in range(num_epochs):
        train_loss_log = AverageMeter()
        train_acc_log = AverageMeter()
        val_loss_log = AverageMeter()
        val_acc_log = AverageMeter()

        for i, (images, labels) in enumerate(train_loader):
            per_layer_representation = []
            images = Variable(images.view(-1, 28 * 28).cuda())
            labels = Variable(labels.cuda())

            optimizer.zero_grad()
            # Gen_net_optimizer.zero_grad()
            outputs = net(images)
            train_loss = criterion(outputs, labels)

            # ========= KI KD 生成网络的输入：模型每层参数的 参数均值 + 梯度均值 ================
            for key, params in net.named_parameters():
                per_layer_grad = torch.autograd.grad(train_loss, params, retain_graph=True)

                per_layer_representation.append(params.mean())
                per_layer_representation.append(per_layer_grad[0].mean())

            # for key, params in KIKD_Gen_Net.named_parameters():
            #     gard_tmp = torch.autograd.grad(train_loss, params, retain_graph=True)

            per_layer_representation = torch.stack(per_layer_representation)
            generated_KI_KD = KIKD_Gen_Net(per_layer_representation)
            generated_I, generated_D = torch.split(generated_KI_KD, split_size_or_sections=num_layers)

            gen_loss = torch.sum(generated_KI_KD ** 2)

            index = 0
            for key, _ in net.named_parameters():
                generated_I_params[key.replace(".", "-")] = generated_I[index]
                generated_D_params[key.replace(".", "-")] = generated_D[index]
                index += 1
            # ===============================================================

            # gradient = torch.autograd.grad(
            #     outputs=train_loss,
            #     inputs=list(KIKD_Gen_Net.parameters()),
            #     allow_unused=True,  # 设置 allow_unused=True
            #     # retain_graph=True
            # )
            #
            # # 获取模型的参数名称与参数值的映射
            # model_state_dict = KIKD_Gen_Net.state_dict()
            #
            # # 打印梯度为None的张量对应的参数名称
            # for i, grad in enumerate(gradient):
            #     if grad is None:
            #         param_name = list(model_state_dict.keys())[i]
            #         print(f"Gradient for parameter '{param_name}' is None")
            #         ==================

            train_loss.backward()
            optimizer.step(gen_I=generated_I_params, gen_D=generated_D_params)

            # gen_loss.backward()
            # Gen_net_optimizer.zero_grad()
            # KIKD_Gen_Net.zero_grad()
            # gen_net_grad = torch.autograd.grad(gen_loss, KIKD_Gen_Net.parameters())
            # --------------- 手动更新生成网络的参数 --------------
            # with torch.no_grad():
            #     for param, grad in zip(KIKD_Gen_Net.parameters(), gen_net_grad):
                    # param -= 1e1 * grad  # 使用梯度下降更新参数
                    # print(KIKD_Gen_Net[0].weight[8][10].item())
            # Gen_net_optimizer.step()

            prec1, prec5 = accuracy(outputs.data, labels.data, topk=(1, 5))
            train_loss_log.update(train_loss.item(), images.size(0))
            train_acc_log.update(prec1[0].cpu(), images.size(0))

            # make_dot(gen_avg_loss, params=dict(KIKD_Gen_Net.named_parameters())).render("graph_gen")
            # break

        # gradient = torch.autograd.grad(
        #             outputs=gen_avg_loss,
        #             inputs=list(KIKD_Gen_Net.parameters()),
        #             allow_unused=True,  # 设置 allow_unused=True
        #             # retain_graph=True
        #             )
        # print(KIKD_Gen_Net.gen_fc1.weight[10][10].item(), net.fc1.weight[100][100].item())

        print('Epoch [%d/%d], Loss: %.4f, Acc: %.3f%%' % (epoch + 1, num_epochs, train_loss_log.avg, train_acc_log.avg))

        # ------------------------ Test the Model --------------------------------
        net.eval()
        correct = 0
        loss = 0
        total = 0

        for images, labels in test_loader:
            images = Variable(images.view(-1, 28 * 28)).cuda()
            labels = Variable(labels).cuda()
            outputs = net(images)
            test_loss = criterion(outputs, labels)
            val_loss_log.update(test_loss.item(), images.size(0))
            prec1, prec5 = accuracy(outputs.data, labels.data, topk=(1, 5))
            val_acc_log.update(prec1[0].cpu(), images.size(0))

        logger.append([learning_rate, train_loss_log.avg, val_loss_log.avg, train_acc_log.avg, val_acc_log.avg])
        print(f'Accuracy of the network on the 10000 test images: {val_acc_log.avg}%, Loss of the network on the 10000 test images: {val_loss_log.avg:.3f}%.')
        print('-' * 150)

    logger.close()
