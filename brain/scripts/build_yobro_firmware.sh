#!/bin/bash
# 編譯 xiaozhi-esp32 韌體，喚醒詞 = "yo bro"（自訂 MultiNet）+ M5Stack CoreS3。
# 繞過壞掉的 ESP-IDF python 依賴檢查（套件其實都裝好了）。
set -e
export IDF_PATH=~/esp/esp-idf
export IDF_PYTHON_CHECK_CONSTRAINTS=no
IDFENV=/Users/USERNAME/.espressif/python_env/idf5.5_py3.9_env
# 工具鏈 bin（xtensa gcc 等）
TOOLBIN=$(find ~/.espressif/tools -type d -name bin 2>/dev/null | tr '\n' ':')
export PATH="$IDFENV/bin:$IDF_PATH/tools:${TOOLBIN}$PATH"
IDF="python $IDF_PATH/tools/idf.py"

cd ~/esp/xiaozhi-esp32

# CoreS3 板型 + yo bro 喚醒詞。注意：CONFIG_SPIRAM_MODE_QUAD 必須蓋過
# sdkconfig.defaults.esp32s3 的 OCT 預設，所以本檔要排在 SDKCONFIG_DEFAULTS 最後。
cat > sdkconfig.defaults.local <<'CFG'
CONFIG_BOARD_TYPE_M5STACK_CORE_S3=y
CONFIG_SPIRAM_MODE_OCT=n
CONFIG_SPIRAM_MODE_QUAD=y
CONFIG_CAMERA_GC0308=y
CONFIG_USE_CUSTOM_WAKE_WORD=y
CONFIG_CUSTOM_WAKE_WORD="yo bro"
CONFIG_CUSTOM_WAKE_WORD_DISPLAY="yo bro"
CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=8
CONFIG_SEND_WAKE_WORD_DATA=y
CONFIG_SR_MN_EN_MULTINET7_QUANT=y
CFG
rm -f sdkconfig
# 本地檔排最後 → 覆寫 target 預設（OCT→QUAD）
export SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.defaults.local"

echo "▶ set-target esp32s3"
$IDF -DSDKCONFIG_DEFAULTS="$SDKCONFIG_DEFAULTS" set-target esp32s3
echo "▶ 確認 PSRAM = QUAD"
grep -E "CONFIG_SPIRAM_MODE_(QUAD|OCT)" sdkconfig | head

echo "▶ 編譯中（約 30-60 分）..."
$IDF -DSDKCONFIG_DEFAULTS="$SDKCONFIG_DEFAULTS" build

echo "▶ 合併單一 bin..."
cd build
python -m esptool --chip esp32s3 merge_bin -o merged-yobro.bin @flash_args
echo "✅ 完成：$(pwd)/merged-yobro.bin"
