from ocr import paddleocr_engine

ocr = paddleocr_engine._get_paddle_instance()

print("=== RAW structure on COLOR (raw) image ===")
result_color = ocr.ocr("data/processed/a77b68364af64072857767e02ca52aa9.jpg", cls=True, rec=False)
print(type(result_color), len(result_color) if result_color else 0)
if result_color and result_color[0]:
    print("first 3 entries:", result_color[0][:3])

print()
print("=== RAW structure on GRAYSCALE (denoised) image ===")
result_gray = ocr.ocr("debug_step4_denoise.jpg", cls=True, rec=False)
print(type(result_gray), len(result_gray) if result_gray else 0)
if result_gray and result_gray[0]:
    print("first 3 entries:", result_gray[0][:3])
