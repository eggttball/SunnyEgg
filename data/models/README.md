# 本機 OpenCV 人臉模型

辨識需要 OpenCV Zoo 的兩個 ONNX 模型。程式不會在執行時偷偷下載模型。

- `face_detection_yunet_2023mar.onnx`
  https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- `face_recognition_sface_2021dec.onnx`
  https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface

下載模型的原始 ONNX 檔放進此資料夾，並保留官方模型授權資訊。
YuNet 偵測人臉位置；SFace 計算本機特徵。幼兒辨識效果需用實際素材校準。
