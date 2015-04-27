#!/usr/bin/env python
# -*- coding: utf-8 -*-
import numpy as np
import cv2
from sklearn import metrics
from sklearn.cross_validation import StratifiedShuffleSplit

from loader import Face, AvgFace
import classifiers

import argparse
import yaml
import multiprocessing as mp
import time
import os
import psutil
import logging as log

log.basicConfig(level=log.INFO)


#def gen_avg(exconf):
    #af = AvgFace(exconf)
    #size = af.exconf["eyefitting"]["size"]
    #total = np.zeros((size[0],size[1],3))
    #total_score = 0
    #n = len(af.faces)
    #print("Total faces: ",n)
    #for face in af.faces[:n]:
        #fe = preproc(face.fit_eyes())
        #total += fe
        #cv2.imwrite("mapped/m_{}.jpg".format(face.image_id),fe)
    #avg = total/n
    #cv2.imwrite("avg.jpg",avg)

class Experiment:
    def __init__(self,exconf):
        self.exconf = exconf
        # Confusion matrix queue
        self.cmq = mp.Queue()
    def new_clf(self):
        name = self.exconf["classifier"]["name"]
        options = self.exconf["classifier"]
        return getattr(classifiers,name)(options)

    def make_validator(self, train_index, test_index):
        xv = XValidator()
        xv.faces_train, xv.y_train = self.af.faces[train_index], self.y[train_index]
        xv.faces_test, xv.y_test = self.af.faces[test_index], self.y[test_index]
        xv.cmq = self.cmq
        xv.clf = self.new_clf()
        return xv
    def report(self):
        y_test_total = []
        y_pred_total = []
        while not self.cmq.empty():
            y_test, y_pred = self.cmq.get()
            y_test_total.append(y_test)
            y_pred_total.append(y_pred)

        y_test_total = np.ravel(y_test_total)
        y_pred_total = np.ravel(y_pred_total)

        def city_fmt(city):
            return "".join([l for l in city if l.isupper()])
        labels = [["M","F"][c["gender"]]+c["country"]+city_fmt(c["city"]) for c in self.exconf["classes"]]

        cm = metrics.confusion_matrix(y_test_total,y_pred_total)
        np.set_printoptions(precision=2)
        log.info("{:*^30}".format("Experiment"))
        log.info("Confusion matrix, without normalization\n"+str(cm))

        cm_normalized = cm.astype("float") / cm.sum(axis=1)[:, np.newaxis]
        log.info("Normalized confusion matrix\n"+str(cm_normalized))

        log.info("Report\n"+str(metrics.classification_report(y_test_total,y_pred_total,target_names=labels)))


    def run(self):
        start_time = time.time()

        self.af = AvgFace(self.exconf)
        self.y = np.array([int(face.c)+1 for face in self.af.faces])
        assert len(np.unique(self.y)) >= 2

        end_time = time.time()
        log.debug("AvgFace done in {:2f}s".format(end_time - start_time))


        xvconf = self.exconf["crossvalidation"]


        skf = StratifiedShuffleSplit(self.y, xvconf["k"], test_size=xvconf["test_size"],random_state=0)
        validators = [self.make_validator(train_index,test_index) for train_index, test_index in skf]


        vgsize = xvconf["vgsize"]
        assert xvconf["k"]%vgsize == 0
        vgn = xvconf["k"]/vgsize

        vgroups = np.array(validators).reshape((vgn,vgsize))

        n_classes = len(np.unique(self.y))
        cm = np.zeros((n_classes,n_classes))
        for vg in vgroups:
            for v in vg:
                v.start()

            for v in vg:
                v.join()
        self.report()


def action_xvalid(conf):
    exs = []
    queue = mp.Queue()
    for i,exconf in enumerate(conf["exs"]):
        ex = Experiment(exconf)
        exs.append(ex)
        ex.run()

def action_avg(conf):
    pass


def mem(pid):
    # return the memory usage in MB
    process = psutil.Process(pid)
    mem = process.get_memory_info()[0] / float(2 ** 20)
    return mem

class XValidator(mp.Process):
    """Parallel validator"""
    def __init__(self):
        mp.Process.__init__(self)
    def run(self):
        pid = os.getpid()
        log.debug("[{}] - started with {:.2f}MB ({}/{} samples)".format(pid,mem(os.getpid()),len(self.y_train),len(self.y_test)))

        def ignore_broken(faces):
            nfaces = []
            for face in faces:
                fitted = face.fit_eyes()
                if fitted is not None:
                    nfaces.append(fitted)
                else:
                    face.broken = True
            return np.array(nfaces)
        self.X_train = ignore_broken(self.faces_train)
        self.X_test = ignore_broken(self.faces_test)
        self.y_train = self.y_train[np.array([not face.broken for face in self.faces_train])]
        self.y_test = self.y_test[np.array([not face.broken for face in self.faces_test])]

        assert len(self.X_train) == len(self.y_train)
        assert len(self.X_test) == len(self.y_test)
        log.debug("[{}] - fit eyes {:.2f}MB".format(pid,mem(os.getpid())))


        start_time = time.time()

        self.clf.fit(self.X_train,self.y_train)
        log.debug("[{}] - fit CLF {:.2f}MB".format(pid,mem(os.getpid())))

        after_train_time = time.time()

        y_pred = self.clf.predict(self.X_test)

        end_time = time.time()

        log.debug("[{}] - trained in {:.2f}s, tested in {:.2f}s, total {:.2f}s"
                .format(pid,after_train_time - start_time,end_time - after_train_time,end_time - start_time))

        self.cmq.put((self.y_test,y_pred))
        
        
def main():
    parser = argparse.ArgumentParser(description="Analysis")
    parser.add_argument("--config",type=str,metavar="EXCONF",required=True,help="YAML configuration")
    parser.add_argument("action",choices=["xvalid","avg"],help="Action to perform")
    args = parser.parse_args()
    conf = yaml.load(open(args.config))
    w_start = time.time()
    if args.action == "xvalid":
        action_xvalid(conf)
    elif args.action == "avg":
        action_avg(conf)
    else: 
        print("Unknown action")
    w_end = time.time()
    log.info("[an] - total {:.2f}s"
            .format(w_end - w_start))

if __name__ == "__main__":
    main()
