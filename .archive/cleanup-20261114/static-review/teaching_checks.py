"""Fixed, dependency-free regression assertions for the teaching corpus."""

import sys


def empty_mean(values):
    return sum(values) / len(values) if values else None


def inclusive_limit(count, limit):
    return count <= limit


def main(fixture):
    if fixture == 'empty_mean':
        try:
            sum([]) / len([])
        except ZeroDivisionError:
            original_failed = True
        else:
            original_failed = False
        passed = original_failed and empty_mean([]) is None and empty_mean([2, 4]) == 3
    elif fixture == 'inclusive_limit':
        count, limit = 4, 4
        original_failed = not count < limit
        passed = original_failed and inclusive_limit(count, limit) and not inclusive_limit(5, 4)
    else:
        return 2
    print(f'{fixture}: {"pass" if passed else "fail"}')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
