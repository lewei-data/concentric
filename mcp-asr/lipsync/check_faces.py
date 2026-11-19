import os
import cv2
import glob

AV = "/home/boray/opc-workbench/prototype/assets/avatars"
cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

for p in sorted(glob.glob(os.path.join(AV, "*.png"))):
    img = cv2.imread(p)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5, minSize=(60, 60))
    print(os.path.basename(p), img.shape, faces.tolist() if len(faces) else "NONE")
