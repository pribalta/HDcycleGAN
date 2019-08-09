import torch as pt
from torch.nn.functional import selu, relu
from torch.nn.parameter import Parameter
from autoencoder import model_parts as parts
from collections import namedtuple


class Generator(pt.nn.Module):

    def __init__(self, scale_factor, n_residual=(6, 3), activation=relu, input_channels=1,
                 channel_factor=8, kernel_size=(3, 3), skip_conn='concat',
                 norm_func=pt.nn.InstanceNorm2d, up_conv=parts.ConvResize2d, pad=pt.nn.ZeroPad2d):
        super().__init__()

        self.skip_conn = skip_conn
        self.downsampling = []
        self.residuals = []
        self.upsampling = []

        # initial 7x7 convolution
        self.initial_conv = parts.GeneralConvolution(input_channels, channel_factor, (7, 7), (1, 1), pt.nn.ZeroPad2d,
                                                     norm_func, activation, pt.nn.Conv2d, True)

        # downsampling with strided convolutions, channels are double after each convolution
        for i in range(scale_factor):
            channel_factor *= 2
            self.downsampling.append(
                parts.GeneralConvolution(channel_factor // 2, channel_factor, kernel_size, (2, 2), pt.nn.ZeroPad2d,
                                         norm_func, activation, pt.nn.Conv2d, True)
            )
            self.add_module(f'down_conv_{i}', self.downsampling[-1])

        # add residual blocks
        for i in range(n_residual[0]):
            self.residuals.append(
                parts.ResBlock2d(channel_factor, n_residual[1], kernel_size, pt.nn.ZeroPad2d, norm_func)
            )
            self.add_module(f'res_block_{i}', self.residuals[-1])

        # upsampling
        for i in range(scale_factor):
            in_channels = channel_factor*2 if skip_conn == 'concat' else channel_factor
            channel_factor = channel_factor // 2
            self.upsampling.append(
                up_conv(in_channels, channel_factor, kernel_size, (1, 1),
                        norm=norm_func, activation=activation, affine=True, padding=pad)
            )
            self.add_module(f'up_conv_{i}', self.upsampling[-1])

        # final convolution
        in_channels = channel_factor * 2 if skip_conn == 'concat' else channel_factor
        self.final_conv = parts.GeneralConvolution(in_channels, input_channels, kernel_size, (1, 1), pt.nn.ZeroPad2d,
                                                   norm_func, activation, pt.nn.Conv2d, True)

    def forward(self, x):

        out = x

        out = self.initial_conv(out)

        skips = []
        for down_conv in self.downsampling:
            skips.append(out)
            out = down_conv(out)
        skips.append(out)

        for residual in self.residuals:
            out = residual(out)

        for up_conv in self.upsampling:
            if self.skip_conn == 'concat':
                out = pt.cat([out, skips.pop()], dim=1)
            elif self.skip_conn == 'add':
                out = out + skips.pop()
            out = up_conv(out)

        if self.skip_conn == 'concat':
            out = pt.cat([out, skips.pop()], dim=1)
        elif self.skip_conn == 'add':
            out = out + skips.pop()
        out = self.final_conv(out)

        return out


class Discriminator(pt.nn.Module):

    def __init__(self, channel_factor=2, n_layers=7, activation=relu, kernel_size=(4, 4),
                 n_residual=(0, 0), max_channels=1024, input_channels=1, affine=False, mode='regression', **kwargs):

        super(Discriminator, self).__init__()
        self.layers = []
        current_channels = input_channels
        for depth_index in range(n_layers):
            out_channels = channel_factor * 2 ** depth_index
            if out_channels > max_channels: out_channels = max_channels
            for res_index in range(n_residual[0]):
                self.layers.append(
                    parts.ResBlock2d(current_channels, n_residual[1], kernel_size, activation=activation, affine=affine, **kwargs))
                self.add_module('r-block{}-{}'.format(depth_index + 1, res_index + 1), self.layers[-1])

            self.layers.append(
                parts.GeneralConvolution(current_channels, out_channels, kernel_size, (2, 2), activation=activation,
                                         padding=pt.nn.ReflectionPad2d, norm=pt.nn.InstanceNorm2d,
                                         convolution=pt.nn.Conv2d, affine=affine, **kwargs))
            self.add_module('conv{}'.format(depth_index + 1), self.layers[-1])
            current_channels = out_channels

        self.layers.append(parts.GlobalAveragePooling2d())
        self.add_module('average-pooling', self.layers[-1])

        if mode == 'regression':

            self.layers.append(pt.nn.Conv2d(out_channels, 1, 1))
            self.add_module('depth-wise-conv', self.layers[-1])

            self.layers.append(parts.Flatten())
            self.add_module('flatten', self.layers[-1])

        elif mode == 'classification':

            self.layers.append(parts.Flatten())
            self.add_module('flatten', self.layers[-1])

            self.layers.append(pt.nn.Linear(current_channels, 3))
            self.add_module('linear', self.layers[-1])

        else:
            raise ValueError('mode should be either "regression" or "classification"')

    def forward(self, x):

        out = x
        for layer in self.layers:
            out = layer(out)

        return out


class ImagePool(pt.nn.Module):

    def __init__(self, size, shape, write_probability=1):

        super(ImagePool, self).__init__()
        self.pool = Parameter(pt.rand(size, *shape), False)
        self.write_probability = write_probability

    def write(self, item):

        if pt.rand(1) <= self.write_probability:

            if item.shape[0] > 1:
                item = item[self.random_index(len(item)), ...]

            self.pool[self.random_index()] = item.detach()

    def sample(self, batch_size):
        samples = []
        for _ in range(batch_size):
            samples.append(self.pool[self.random_index()])
        samples = pt.cat(samples)
        return samples

    def random_index(self, size=None):

        size = size if size is not None else len(self.pool)
        return (pt.rand(1)*size).long()


class HDCycleGAN(pt.nn.Module):

    def __init__(self, generator, discriminator, input_size, pool_size=64, pool_write_probability=1):

        super(HDCycleGAN, self).__init__()

        self.discriminator = discriminator[0](**discriminator[1])
        self.generator = {'hn': generator[0](**generator[1]),
                          'ln': generator[0](**generator[1])}

        self.add_module('discriminator', self.discriminator)
        self.add_module('generator_ln', self.generator['ln'])
        self.add_module('generator_hn', self.generator['hn'])

        self.pool_size = pool_size
        self.pool_write_probability = pool_write_probability
        self.pool = {'ln': ImagePool(self.pool_size, input_size, write_probability=self.pool_write_probability),
                     'hn': ImagePool(self.pool_size, input_size, write_probability=self.pool_write_probability)}

    def generate(self, x, quality):
        return self.generator[quality](x)

    def discriminate(self, x):
        return self.discriminator(x)

    def cycle(self, x, start_quality):
        other = 'hn' if start_quality == 'ln' else 'ln'
        return self.generate(self.generate(x, other), start_quality)

    def discriminate_from_pool(self, quality, batch_size):
        return self.discriminate(self.pool[quality].sample(batch_size))

    def _forward(self, x, target_quality):

        generated = self.generate(x, target_quality)
        self.pool[target_quality].write(generated)

        real = self.discriminate(x)
        fake = self.discriminate(generated)
        pool_fake = self.discriminate_from_pool(target_quality, len(generated))

        scores = namedtuple('scores', ('real', 'fake', 'pool_fake'))  # caution: real score is for a sample from the other domain
        prediction = namedtuple(target_quality, ('generated', 'scores'))

        return prediction(generated, scores(real, fake, pool_fake))

    def forward(self, x):

        hn, ln = x

        generated_ln, prediction_ln = self._forward(hn, 'ln')
        generated_hn, prediction_hn = self._forward(ln, 'hn')
        cycled = self.generate(generated_ln, 'hn'), self.generate(generated_hn, 'ln')

        result = namedtuple('Result', ('cycled', 'hn_scores', 'ln_scores'))

        return result(cycled, prediction_hn, prediction_ln)