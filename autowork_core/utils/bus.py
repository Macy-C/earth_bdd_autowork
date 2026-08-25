import ctypes
import os
import random
import string
import time
import yaml
from config.paths import Paths


def _convert(string, ignore, caseless, spaceless):
    """Normalizes given string according to given spec.

    By default string is turned to lower case and all whitespace is removed.
    Additional characters can be removed by giving them in ``ignore`` list.
    """

    empty = '' if isinstance(string, str) else b''
    if isinstance(ignore, bytes):
        # Iterating bytes in Python3 yields integers.
        ignore = [bytes([i]) for i in ignore]
    if spaceless:
        string = empty.join(string.split())
    if caseless:
        string = string.lower()
        ignore = [i.lower() for i in ignore]
    # both if statements below enhance performance a little
    if ignore:
        for ign in ignore:
            if ign in string:
                string = string.replace(ign, empty)
    return string


def normalize(string, ignore=(), caseless=True, spaceless=True):

    if isinstance(string,list) or isinstance(string,set):
        if isinstance(string,set):
            string = list(string)
        listA = string
        listB = list()
        for i in range(0, len(listA)):
            temp = _convert(string=listA[i],ignore=ignore, caseless=caseless, spaceless=spaceless)
            listB.append(temp)
        return listB
    else:
        return _convert(string=string,ignore=ignore, caseless=caseless, spaceless=spaceless)

def get_yaml_data(file_path):
    with open(file_path, 'r', encoding='UTF-8') as f:
        temp_config = yaml.load(f, Loader=yaml.SafeLoader)
        return temp_config if temp_config else dict()

def timestamp():
    now = int(time.time())
    timeArray = time.localtime(now)
    return time.strftime("%Y%m%d%H%M%S", timeArray)

def safe_name(name):
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']

    for ch in invalid_chars:
        name = name.replace(ch, "_")

    return name

def random_str_ABC(num):
    """
        返回指定位数随机大写字母
        """
    ran_str = ''.join(random.sample(string.ascii_uppercase, num))
    return ran_str


def random_num_and_str(num):
    """
    返回指定位数随机大小写字母和数字组合
    """
    ran_str = ''.join(random.sample(string.ascii_letters + string.digits, num))
    return ran_str


def random_num(num):
    """
        返回指定位数随机数字
        """
    ran_str = ''.join(random.sample(string.digits+string.digits, num))
    return ran_str


def random_str(num):
    """
        返回指定位数随机大小写字母
        """
    ran_str = ''.join(random.sample(string.ascii_letters, num))
    return ran_str

def find_nth_occurrence(string, substring, n):
    """
    查找字符串中字符第n次出现的下标
    """
    start = string.find(substring)

    while start >= 0 and n > 1:
        start = string.find(substring, start + 1)
        n -= 1

    return start


def random_list(list, num):
    """
            返回列表内随机元素，
            """
    # ran_str = ''.join(random.sample(list, num))
    res = []

    def ran():
        ran_str = random.choice(list)
        if ran_str in res:
            ran()
        else:
            res.append(ran_str)

    for i in range(num):
        ran()
    return res


def random_phone(num):
    """
    随机生成手机号
    num: 生成的个数
    """
    res = []
    list = ['134', '135', '136', '137', '138', '139', '150', '151', '152', '158', '159', '157', '182', '187', '188',
            '147', '130', '131', '132', '155', '156', '185', '186', '133', '153', '180', '189']

    def ran():
        head = random_list(list, 1)
        phone = head[0] + random_num(8)
        if phone in res:
            ran()
        else:
            res.append(phone)

    for i in range(num):
        ran()
    return res


def random_License_plate():
    # 生成省份缩写
    provinces = ["京", "津", "沪", "渝", "冀", "晋", "辽", "吉", "黑", "苏", "浙", "皖", "闽", "赣", "鲁", "豫", "鄂",
                 "湘", "粤", "桂", "琼", "川", "贵", "云", "陕", "甘", "青", "宁", "新"]
    province = random.choice(provinces)

    # 生成车牌号中的数字部分
    numbers = "".join(str(random.randint(0, 9)) for _ in range(5))

    # 生成车牌号中的字母部分
    letters = "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(1))

    # 组合成车牌号
    license_plate = province + letters + numbers
    return license_plate



def create_phone(num, path):
    list = random_phone(num)

    with open(path + 'phone.txt', 'w') as f:
        for i in list:
            f.write(i + '\n')


def del_all_file(path):
    '''
    删除目录下所有文件
    :param path: 目录路径
    :return:
    '''
    if os.path.exists(path):
        ls = os.listdir(path)
        for i in ls:
            if 'dog.png' in i:
                continue
            c_path = os.path.join(path, i)
            if os.path.isdir(c_path):
                del_all_file(c_path)
            else:
                os.remove(c_path)
    else:
        print(f'^^^^^^^路径没找到：{path}')


def del_file_name(path,file):
    ls = os.listdir(path)
    for i in ls:
        c_path = os.path.join(path, i)
        if os.path.isdir(c_path):
            del_file_name(c_path,file)
        else:
            if file in str(c_path):
                os.remove(c_path)


def get_screen_size():
    try:
        # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    return width, height


if __name__ == '__main__':
    # del_all_file(Paths.SCREENSHOTS_DIR)
    # del_all_file(Paths.REPORTS_DIR / 'allure-results')
    print(get_screen_size())




