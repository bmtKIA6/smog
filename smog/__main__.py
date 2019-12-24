import itertools
import os
import sys

import trio

from smog.api import SmugMugApi
from smog.index import AlbumIndex, DirectoryIndex


# TODO refactor pagination logic
async def list_nodes(api, next_page):
    while next_page:
        nodes_response = await api.list_nodes(next_page)
        for node in nodes_response['Response']['Node']:
            yield node
        next_page = nodes_response['Response']['Pages'].get('NextPage')


async def run_operation(limit, progress, msg, fn, *args):
    async with limit:
        print(msg, progress)
        progress[0] += 1
        await fn(*args)


async def create_album(api, folder_node_endpoint, dir_index, index_root, to_sync):
    album_node_response = await api.create_album_node(folder_node_endpoint, dir_index.dir_path.name)
    album_endpoint = album_node_response['Response']['Node']['Uris']['Album']
    albumkey = album_endpoint.split('/')[-1]
    await dir_index.set_albumkey(albumkey)
    album_index = AlbumIndex(index_root / albumkey, api, album_endpoint)
    to_sync.append((dir_index, album_index))


async def main():
    _, index_root, *dirs = sys.argv

    oauth_consumer_key = os.environ['SMUGMUG_API_KEY']
    oauth_consumer_secret = os.environ['SMUGMUG_API_SECRET']
    oauth_token = os.environ['SMUGMUG_OAUTH_ACCESS_TOKEN']
    oauth_token_secret = os.environ['SMUGMUG_OAUTH_TOKEN_SECRET']

    api = SmugMugApi(oauth_consumer_key, oauth_consumer_secret,
                     oauth_token, oauth_token_secret)
    limit = trio.CapacityLimiter(8)

    dir_by_name = {}
    dir_by_albumkey = {}
    for dir_path in dirs:
        dir_index = DirectoryIndex(dir_path)
        albumkey = await dir_index.get_albumkey()
        if albumkey is None:
            dir_by_name[dir_index.dir_path.name] = dir_index
        elif albumkey in dir_by_albumkey:
            raise Exception('duplicate album key', albumkey)
        else:
            dir_by_albumkey[albumkey] = dir_index

    to_sync = []
    index_root = trio.Path(index_root)
    authuser_response = await api.get_authuser()
    folder_node_endpoint = authuser_response['Response']['User']['Uris']['Node']

    async for node in list_nodes(api, folder_node_endpoint):
        if node['Type'] != 'Album':
            continue
        albumkey = node['Uris']['Album'].split('/')[-1]
        album_index = AlbumIndex(index_root / albumkey, api, node['Uris']['Album'])
        # TODO unhandled edge case where albumkey misses but name hits
        if albumkey in dir_by_albumkey:
            dir_index = dir_by_albumkey.pop(albumkey)
            to_sync.append((dir_index, album_index))
        elif node['Name'] in dir_by_name:
            dir_index = dir_by_name.pop(node['Name'])
            to_sync.append((dir_index, album_index))
        else:
            # TODO set an album keyword?
            print('Unlinked album', albumkey)
            continue

    progress = [0, len(dir_by_albumkey) + len(dir_by_name)]
    async with trio.open_nursery() as nursery:
        for dir_index in itertools.chain(dir_by_albumkey.values(), dir_by_name.values()):
            nursery.start_soon(run_operation, limit, progress,
                               f'Creating album {dir_index.dir_path.name}',
                               create_album, api, folder_node_endpoint, dir_index, index_root, to_sync)

    progress = [0, 2 * len(to_sync)]
    async with trio.open_nursery() as nursery:
        for dir_index, album_index in to_sync:
            nursery.start_soon(run_operation, limit, progress,
                               f'Reindexing {dir_index.dir_path}',
                               dir_index.reindex)
            nursery.start_soon(run_operation, limit, progress,
                               f'Reindexing {album_index.album_endpoint}',
                               album_index.reindex)

    operations = []
    for dir_index, album_index in to_sync:
        # list() does not seem to work on async generators
        dir_by_md5 = [x async for x in dir_index.iter_by_md5()]
        album_by_md5 = [x async for x in album_index.iter_by_md5()]
        for x in (dir_by_md5, album_by_md5):
            x.append(('x', None)) # sentinel
        dir_idx = album_idx = 0
        while dir_idx < len(dir_by_md5) and album_idx < len(album_by_md5):
            dir_md5, image_filename = dir_by_md5[dir_idx]
            album_md5, image_endpoint = album_by_md5[album_idx]
            if dir_md5 == album_md5:
                dir_idx += 1
                album_idx += 1
            elif dir_md5 < album_md5:
                image_path = dir_index.dir_path / image_filename
                operations.append((f'Uploading {image_path}',
                                   api.upload_image, album_index.album_endpoint, image_path))
                dir_idx += 1
            else:
                # TODO keywords handled sloppily. should add not overwrite. never unset
                operations.append((f'Marking for removal {image_endpoint}',
                                   api.set_image_keywords,
                                   image_endpoint, 'smog-upload; smog-removed'))
                album_idx += 1
        assert dir_idx == len(dir_by_md5) and album_idx == len(album_by_md5)

    # TODO this is such a janky and probably incorrectly synchronized way to share data
    progress = [0, len(operations)]
    async with trio.open_nursery() as nursery:
        for msg, fn, *args in operations:
            nursery.start_soon(run_operation, limit, progress, msg, fn, *args)

    print('done')


if __name__ == '__main__':
    trio.run(main)
