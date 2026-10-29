import Foundation
import Vision
import ImageIO
while let line = readLine() {
 do {
  let input = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String]
  var output: [[String:Any]] = []
  for file in input {
   let request = VNRecognizeTextRequest()
   request.recognitionLevel = .accurate
   request.usesLanguageCorrection = false
   request.recognitionLanguages = ["en-US"]
   request.regionOfInterest = CGRect(x: 0, y: 0, width: 1, height: 0.25)
   let handler = VNImageRequestHandler(url: URL(fileURLWithPath: file), options: [:])
   try handler.perform([request])
   let rows = (request.results ?? []).compactMap { item -> [String:Any]? in
    guard let text = item.topCandidates(1).first else { return nil }
    return ["text":text.string,"confidence":text.confidence,"x":item.boundingBox.minX,"y":item.boundingBox.minY,"w":item.boundingBox.width,"h":item.boundingBox.height]
   }
   output.append(["file":file,"rows":rows])
  }
  let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
  print(String(data:data,encoding:.utf8)!)
  fflush(stdout)
 } catch { print("{\"error\":\"ocr_failed\"}");fflush(stdout) }
}
