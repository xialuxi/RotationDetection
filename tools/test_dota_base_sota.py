# -*- coding:utf-8 -*-
# Author: Xue Yang <yangxue-2019-sjtu@sjtu.edu.cn>, <yangxue0827@126.com>
# License: Apache-2.0 license
# Copyright (c) SJTU. ALL rights reserved.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import math
import os
from multiprocessing import Queue, Process

import cv2
import numpy as np
import tensorflow as tf
from alpharotate.libs.utils.rotate_polygon_nms import rotate_gpu_nms
from tqdm import tqdm

from alpharotate.libs.label_name_dict.label_dict import LabelMap
from alpharotate.libs.utils import nms_rotate
from alpharotate.libs.utils.coordinate_convert import forward_convert, backward_convert
from alpharotate.libs.utils.draw_box_in_img import DrawBox
from alpharotate.utils import tools
from alpharotate.utils.pretrain_zoo import PretrainModelZoo


def parse_args():
    parser = argparse.ArgumentParser('Start testing.')

    parser.add_argument('--test_dir', dest='test_dir',
                        help='evaluate imgs dir ',
                        default='/data/dataset_share/DOTA/test/images/', type=str)
    parser.add_argument('--gpus', dest='gpus',
                        help='gpu id',
                        default='0,1,2,3,4,5,6,7', type=str)
    parser.add_argument('--show_box', '-s', default=False,
                        action='store_true')
    parser.add_argument('--multi_scale', '-ms', default=False,
                        action='store_true')
    parser.add_argument('--flip_img', '-f', default=False,
                        action='store_true')
    parser.add_argument('--cpu_nms', '-cn', default=False,
                        action='store_true')
    parser.add_argument('--num_imgs', dest='num_imgs',
                        help='test image number',
                        default=np.inf, type=int)
    parser.add_argument('--h_len', dest='h_len',
                        help='image height',
                        default=[600, 800, 1024, 1300, 1600], type=list)
    parser.add_argument('--w_len', dest='w_len',
                        help='image width',
                        default=[600, 800, 1024, 1300, 1600], type=list)
    parser.add_argument('--h_overlap', dest='h_overlap',
                        help='height overlap',
                        default=[150, 200, 300, 300, 400], type=list)
    parser.add_argument('--w_overlap', dest='w_overlap',
                        help='width overlap',
                        default=[150, 200, 300, 300, 400], type=list)
    args = parser.parse_args()
    return args


class TestDOTA(object):

    def __init__(self, cfgs):
        self.cfgs = cfgs
        self.args = parse_args()
        print('+-' * 40)
        print(self.args)
        print('+-' * 40)
        label_map = LabelMap(cfgs)
        self.name_label_map, self.label_name_map = label_map.name2label(), label_map.label2name()

    def worker(self, gpu_id, images, det_net, result_queue):
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)

        img_plac = tf.placeholder(dtype=tf.uint8, shape=[None, None, 3])  # is RGB. not BGR
        img_batch = tf.cast(img_plac, tf.float32)

        pretrain_zoo = PretrainModelZoo()
        if self.cfgs.NET_NAME in pretrain_zoo.pth_zoo or self.cfgs.NET_NAME in pretrain_zoo.mxnet_zoo:
            img_batch = (img_batch / 255 - tf.constant(self.cfgs.PIXEL_MEAN_)) / tf.constant(self.cfgs.PIXEL_STD)
        else:
            img_batch = img_batch - tf.constant(self.cfgs.PIXEL_MEAN)

        img_batch = tf.expand_dims(img_batch, axis=0)

        detection_boxes, detection_scores, detection_category = det_net.build_whole_detection_network(
            input_img_batch=img_batch)

        init_op = tf.group(
            tf.global_variables_initializer(),
            tf.local_variables_initializer()
        )

        restorer, restore_ckpt = det_net.get_restorer()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        with tf.Session(config=config) as sess:
            sess.run(init_op)
            if not restorer is None:
                restorer.restore(sess, restore_ckpt)
                print('restore model %d ...' % gpu_id)

            for img_path in images:

                # if 'P0302' not in img_path:
                #     continue

                img = cv2.imread(img_path)
                # img = np.load(img_path.replace('images', 'npy').replace('.png', '.npy'))

                box_res_rotate = []
                label_res_rotate = []
                score_res_rotate = []

                imgH = img.shape[0]
                imgW = img.shape[1]

                for h_len, w_len, h_overlap, w_overlap in zip(self.args.h_len, self.args.w_len, self.args.h_overlap, self.args.w_overlap):

                    img_short_side_len_list = self.cfgs.IMG_SHORT_SIDE_LEN if isinstance(self.cfgs.IMG_SHORT_SIDE_LEN, list) else [
                        self.cfgs.IMG_SHORT_SIDE_LEN]
                    img_short_side_len_list = [img_short_side_len_list[0]] if not self.args.multi_scale else img_short_side_len_list

                    if imgH < h_len:
                        temp = np.zeros([h_len, imgW, 3], np.float32)
                        temp[0:imgH, :, :] = img
                        img = temp
                        imgH = h_len

                    if imgW < w_len:
                        temp = np.zeros([imgH, w_len, 3], np.float32)
                        temp[:, 0:imgW, :] = img
                        img = temp
                        imgW = w_len

                    for hh in range(0, imgH, h_len - h_overlap):
                        if imgH - hh - 1 < h_len:
                            hh_ = imgH - h_len
                        else:
                            hh_ = hh
                        for ww in range(0, imgW, w_len - w_overlap):
                            if imgW - ww - 1 < w_len:
                                ww_ = imgW - w_len
                            else:
                                ww_ = ww
                            src_img = img[hh_:(hh_ + h_len), ww_:(ww_ + w_len), :]

                            for short_size in img_short_side_len_list:
                                max_len = self.cfgs.IMG_MAX_LENGTH
                                if h_len < w_len:
                                    new_h, new_w = short_size, min(int(short_size * float(w_len) / h_len), max_len)
                                else:
                                    new_h, new_w = min(int(short_size * float(h_len) / w_len), max_len), short_size
                                img_resize = cv2.resize(src_img, (new_w, new_h))

                                resized_img, det_boxes_r_, det_scores_r_, det_category_r_ = \
                                    sess.run(
                                        [img_batch, detection_boxes, detection_scores, detection_category],
                                        feed_dict={img_plac: img_resize[:, :, ::-1]}
                                    )

                                resized_h, resized_w = resized_img.shape[1], resized_img.shape[2]
                                src_h, src_w = src_img.shape[0], src_img.shape[1]

                                if len(det_boxes_r_) > 0:
                                    det_boxes_r_ = forward_convert(det_boxes_r_, False)
                                    det_boxes_r_[:, 0::2] *= (src_w / resized_w)
                                    det_boxes_r_[:, 1::2] *= (src_h / resized_h)

                                    for ii in range(len(det_boxes_r_)):
                                        box_rotate = det_boxes_r_[ii]
                                        box_rotate[0::2] = box_rotate[0::2] + ww_
                                        box_rotate[1::2] = box_rotate[1::2] + hh_
                                        box_res_rotate.append(box_rotate)
                                        label_res_rotate.append(det_category_r_[ii])
                                        score_res_rotate.append(det_scores_r_[ii])

                                if self.args.flip_img:
                                    det_boxes_r_flip, det_scores_r_flip, det_category_r_flip = \
                                        sess.run(
                                            [detection_boxes, detection_scores, detection_category],
                                            feed_dict={img_plac: cv2.flip(img_resize, flipCode=1)[:, :, ::-1]}
                                        )
                                    if len(det_boxes_r_flip) > 0:
                                        det_boxes_r_flip = forward_convert(det_boxes_r_flip, False)
                                        det_boxes_r_flip[:, 0::2] *= (src_w / resized_w)
                                        det_boxes_r_flip[:, 1::2] *= (src_h / resized_h)

                                        for ii in range(len(det_boxes_r_flip)):
                                            box_rotate = det_boxes_r_flip[ii]
                                            box_rotate[0::2] = (src_w - box_rotate[0::2]) + ww_
                                            box_rotate[1::2] = box_rotate[1::2] + hh_
                                            box_res_rotate.append(box_rotate)
                                            label_res_rotate.append(det_category_r_flip[ii])
                                            score_res_rotate.append(det_scores_r_flip[ii])

                                    det_boxes_r_flip, det_scores_r_flip, det_category_r_flip = \
                                        sess.run(
                                            [detection_boxes, detection_scores, detection_category],
                                            feed_dict={img_plac: cv2.flip(img_resize, flipCode=0)[:, :, ::-1]}
                                        )
                                    if len(det_boxes_r_flip) > 0:
                                        det_boxes_r_flip = forward_convert(det_boxes_r_flip, False)
                                        det_boxes_r_flip[:, 0::2] *= (src_w / resized_w)
                                        det_boxes_r_flip[:, 1::2] *= (src_h / resized_h)

                                        for ii in range(len(det_boxes_r_flip)):
                                            box_rotate = det_boxes_r_flip[ii]
                                            box_rotate[0::2] = box_rotate[0::2] + ww_
                                            box_rotate[1::2] = (src_h - box_rotate[1::2]) + hh_
                                            box_res_rotate.append(box_rotate)
                                            label_res_rotate.append(det_category_r_flip[ii])
                                            score_res_rotate.append(det_scores_r_flip[ii])

                box_res_rotate = np.array(box_res_rotate)
                label_res_rotate = np.array(label_res_rotate)
                score_res_rotate = np.array(score_res_rotate)

                box_res_rotate_ = []
                label_res_rotate_ = []
                score_res_rotate_ = []
                threshold = {'roundabout': 0.1, 'tennis-court': 0.3, 'swimming-pool': 0.05, 'storage-tank': 0.2,
                             'soccer-ball-field': 0.3, 'small-vehicle': 0.2, 'ship': 0.2, 'plane': 0.15,
                             'large-vehicle': 0.1, 'helicopter': 0.2, 'harbor': 0.0001, 'ground-track-field': 0.3,
                             'bridge': 0.0001, 'basketball-court': 0.3, 'baseball-diamond': 0.1,
                             'container-crane': 0.05, 'airport': 0.5, 'helipad': 0.1}

                for sub_class in range(1, self.cfgs.CLASS_NUM + 1):
                    index = np.where(label_res_rotate == sub_class)[0]
                    if len(index) == 0:
                        continue
                    tmp_boxes_r = box_res_rotate[index]
                    tmp_label_r = label_res_rotate[index]
                    tmp_score_r = score_res_rotate[index]

                    tmp_boxes_r_ = backward_convert(tmp_boxes_r, False)

                    # cpu nms better than gpu nms (default)
                    if self.args.cpu_nms:
                        try:
                            inx = nms_rotate.nms_rotate_cpu(boxes=np.array(tmp_boxes_r_),
                                                            scores=np.array(tmp_score_r),
                                                            iou_threshold=threshold[self.label_name_map[sub_class]],
                                                            max_output_size=5000)

                        except:
                            tmp_boxes_r_ = np.array(tmp_boxes_r_)
                            tmp = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                            tmp[:, 0:-1] = tmp_boxes_r_
                            tmp[:, -1] = np.array(tmp_score_r)
                            # Note: the IoU of two same rectangles is 0
                            jitter = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                            jitter[:, 0] += np.random.rand(tmp_boxes_r_.shape[0], ) / 1000
                            inx = rotate_gpu_nms(np.array(tmp, np.float32) + np.array(jitter, np.float32),
                                                 float(threshold[self.label_name_map[sub_class]]), 0)
                    else:
                        tmp_boxes_r_ = np.array(tmp_boxes_r_)
                        tmp = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                        tmp[:, 0:-1] = tmp_boxes_r_
                        tmp[:, -1] = np.array(tmp_score_r)
                        # Note: the IoU of two same rectangles is 0
                        jitter = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                        jitter[:, 0] += np.random.rand(tmp_boxes_r_.shape[0], ) / 1000
                        inx = rotate_gpu_nms(np.array(tmp, np.float32) + np.array(jitter, np.float32),
                                             float(threshold[self.label_name_map[sub_class]]), 0)

                    box_res_rotate_.extend(np.array(tmp_boxes_r)[inx])
                    score_res_rotate_.extend(np.array(tmp_score_r)[inx])
                    label_res_rotate_.extend(np.array(tmp_label_r)[inx])

                result_dict = {'boxes': np.array(box_res_rotate_), 'scores': np.array(score_res_rotate_),
                               'labels': np.array(label_res_rotate_), 'image_id': img_path}
                result_queue.put_nowait(result_dict)

    def test_dota(self, det_net, real_test_img_list, txt_name):

        save_path = os.path.join('./test_dota', self.cfgs.VERSION)

        nr_records = len(real_test_img_list)
        pbar = tqdm(total=nr_records)
        gpu_num = len(self.args.gpus.strip().split(','))

        nr_image = math.ceil(nr_records / gpu_num)
        result_queue = Queue(500)
        procs = []

        for i, gpu_id in enumerate(self.args.gpus.strip().split(',')):
            start = i * nr_image
            end = min(start + nr_image, nr_records)
            split_records = real_test_img_list[start:end]
            proc = Process(target=self.worker, args=(int(gpu_id), split_records, det_net, result_queue))
            print('process:%d, start:%d, end:%d' % (i, start, end))
            proc.start()
            procs.append(proc)

        for i in range(nr_records):
            res = result_queue.get()

            if self.args.show_box:

                nake_name = res['image_id'].split('/')[-1]
                tools.makedirs(os.path.join(save_path, 'dota_img_vis'))
                draw_path = os.path.join(save_path, 'dota_img_vis', nake_name)

                draw_img = np.array(cv2.imread(res['image_id']), np.float32)
                detected_boxes = backward_convert(res['boxes'], with_label=False)

                detected_indices = res['scores'] >= self.cfgs.VIS_SCORE
                detected_scores = res['scores'][detected_indices]
                detected_boxes = detected_boxes[detected_indices]
                detected_categories = res['labels'][detected_indices]

                drawer = DrawBox(self.cfgs)

                final_detections = drawer.draw_boxes_with_label_and_scores(draw_img,
                                                                           boxes=detected_boxes,
                                                                           labels=detected_categories,
                                                                           scores=detected_scores,
                                                                           method=1,
                                                                           is_csl=True,
                                                                           in_graph=False)
                cv2.imwrite(draw_path, final_detections)

                # pseudo label txt
                # tools.makedirs(os.path.join(save_path, 'dota_test_txt'))
                # txt_path = os.path.join(save_path, 'dota_test_txt', nake_name.split('.')[0]+'.txt')
                # fw_txt = open(txt_path, 'w')
                # for i, rbox in enumerate(res['boxes'][detected_indices]):
                #     command = '%.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %s 0\n'% (rbox[0], rbox[1], rbox[2], rbox[3],
                #                                                                  rbox[4], rbox[5], rbox[6], rbox[7],
                #                                                                  self.label_name_map[detected_categories[i]]
                #                                                                  )
                #     fw_txt.write(command)
                # fw_txt.close()


            else:
                CLASS_DOTA = self.name_label_map.keys()
                write_handle = {}

                tools.makedirs(os.path.join(save_path, 'dota_res'))
                for sub_class in CLASS_DOTA:
                    if sub_class == 'back_ground':
                        continue
                    write_handle[sub_class] = open(os.path.join(save_path, 'dota_res', 'Task1_%s.txt' % sub_class), 'a+')

                for i, rbox in enumerate(res['boxes']):
                    command = '%s %.3f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f\n' % (res['image_id'].split('/')[-1].split('.')[0],
                                                                                     res['scores'][i],
                                                                                     rbox[0], rbox[1], rbox[2], rbox[3],
                                                                                     rbox[4], rbox[5], rbox[6], rbox[7],)
                    write_handle[self.label_name_map[res['labels'][i]]].write(command)

                for sub_class in CLASS_DOTA:
                    if sub_class == 'back_ground':
                        continue
                    write_handle[sub_class].close()

                fw = open(txt_name, 'a+')
                fw.write('{}\n'.format(res['image_id'].split('/')[-1]))
                fw.close()

            pbar.set_description("Test image %s" % res['image_id'].split('/')[-1])

            pbar.update(1)

        for p in procs:
            p.join()

    def get_test_image(self):
        txt_name = '{}.txt'.format(self.cfgs.VERSION)
        if not self.args.show_box:
            if not os.path.exists(txt_name):
                fw = open(txt_name, 'w')
                fw.close()

            fr = open(txt_name, 'r')
            img_filter = fr.readlines()
            print('****************************' * 3)
            print('Already tested imgs:', img_filter)
            print('****************************' * 3)
            fr.close()

            test_imgname_list = [os.path.join(self.args.test_dir, img_name) for img_name in os.listdir(self.args.test_dir)
                                 if img_name.endswith(('.jpg', '.png', '.jpeg', '.tif', '.tiff')) and
                                 (img_name + '\n' not in img_filter)]
        else:
            test_imgname_list = [os.path.join(self.args.test_dir, img_name) for img_name in os.listdir(self.args.test_dir)
                                 if img_name.endswith(('.jpg', '.png', '.jpeg', '.tif', '.tiff'))]

        assert len(test_imgname_list) != 0, 'test_dir has no imgs there.' \
                                            ' Note that, we only support img format of (.jpg, .png, and .tiff) '

        if self.args.num_imgs == np.inf:
            real_test_img_list = test_imgname_list
        else:
            real_test_img_list = test_imgname_list[: self.args.num_imgs]

        return real_test_img_list


