import asyncio
import re  # for NG songs
import time  # for perf_counter in ping

from itertools import chain

from ._typing import (
    AbstractUser,
    Any,
    Client,
    Comment,
    Dict,
    FriendRequest,
    Gauntlet,
    Iterable,
    Level,
    LevelRecord,
    List,
    MapPack,
    Message,
    Optional,
    Sequence,
    Song,
    Tuple,
    Union,
    User,
    UserStats,
)

from .classconverter import ClassConverter
from .errors import (
    MissingAccess,
    SongRestrictedForUsage,
    NothingFound,
    LoginFailure,
)

from .utils.converter import Converter
from .utils.decorators import check_logged_obj
from .utils.enums import (
    CommentStrategy,
    DemonDifficulty,
    LeaderboardStrategy,
    LevelLeaderboardStrategy,
    SearchStrategy,
)
from .utils.filters import Filters
from .utils.http_request import http
from .utils.indexer import Index
from .utils.params import Parameters as Params
from .utils.parser import Parser
from .utils.routes import Route
from .utils.save_parser import SaveParser
from .utils.crypto.coders import Coder

from . import api
from . import utils


class GDSession:
    """Implements all requests-related functionality.
    No docstrings here yet...
    """
    async def ping_server(self, link: str) -> float:
        start = time.perf_counter()
        await http.normal_request(link)
        end = time.perf_counter()
        return round((end - start) * 1000, 2)

    async def get_song(self, song_id: int = 0) -> Song:
        parameters = Params().create_new().put_definer('song', song_id).finish()
        codes = {
            -1: MissingAccess(message='No songs were found with ID: {}.'.format(song_id)),
            -2: SongRestrictedForUsage(song_id)
        }
        resp = await http.request(Route.GET_SONG_INFO, parameters, error_codes=codes)
        parsed = Parser().with_split('~|~').should_map().parse(resp)
        return ClassConverter.song_convert(parsed)

    async def get_ng_song(self, song_id: int = 0) -> Song:
        # just like get_song(), but gets anything available on NG.
        song_id = int(song_id)  # ensure type

        link = Route.NEWGROUNDS_SONG_LISTEN + str(song_id)

        resp = await http.normal_request(link)
        content = await resp.content.read()
        html = content.decode().replace('\\', '')

        RE = (
            r'https://audio\.ngfiles\.com/([^\'"]+)',  # searching for link
            r'.filesize.:(\d+)',  # searching for size
            r'<title>([^<>]+)</title>',  # searching for name
            r'.artist.:.([^\'"]+).'  # searching for author
        )
        try:
            dl_link = re.search(RE[0], html).group(0)
            size_b = int(re.search(RE[1], html).group(1))  # in B
            size_mb = round(size_b / 1024 / 1024, 2)  # in MB (rounded)
            name = re.search(RE[2], html).group(1)
            author = re.search(RE[3], html).group(1)
        except AttributeError:  # if re.search returned None -> Song not found
            raise MissingAccess(message='No song found under ID: {}.'.format(song_id))

        return ClassConverter.song_from_kwargs(
            name=name, author=author, id=song_id, size=size_mb,
            links=dict(normal=link, download=dl_link), custom=True
        )

    async def get_user(
        self, account_id: int = 0, return_only_stats: bool = False, *, client: Client
    ) -> Union[UserStats, User]:
        parameters = Params().create_new().put_definer('user', account_id).finish()
        codes = {
            -1: MissingAccess(message='No users were found with ID: {}.'.format(account_id))
        }

        resp = await http.request(Route.GET_USER_INFO, parameters, error_codes=codes)
        resp = Parser().with_split(':').should_map().parse(resp)

        if return_only_stats:
            return ClassConverter.user_stats_convert(resp, client)

        another = (
            Params().create_new().put_definer('search', resp.get(Index.USER_PLAYER_ID))
            .put_total(0).put_page(0).finish()
        )
        some_resp = await http.request(Route.USER_SEARCH, another)

        new_resp = (
            Parser().split('#').take(0).check_empty().split(':')
            .should_map().parse(some_resp)
        )

        if new_resp is None:
            return

        resp.update({
            k: new_resp.get(k) for k in (Index.USER_NAME, Index.USER_ICON, Index.USER_ICON_TYPE)
        })

        return ClassConverter.user_convert(resp, client)

    def to_user(self, conv_dict: Optional[Dict[str, Any]] = None, *, client: Client) -> AbstractUser:
        if conv_dict is None:
            conv_dict = {}

        return ClassConverter.abstractuser_convert(conv_dict, client=client)

    async def search_user(
        self, param: Union[int, str],
        return_abstract: bool = False, *, client: Client
    ) -> Union[AbstractUser, User]:

        parameters = (
            Params().create_new().put_definer('search', param)
            .put_total(0).put_page(0).finish()
        )
        codes = {
            -1: MissingAccess(message='Searching for {} returned -1.'.format(param))
        }

        resp = await http.request(Route.USER_SEARCH, parameters, error_codes=codes)
        mapped = (
            Parser().split('#').take(0).check_empty().split(':')
            .should_map().parse(resp)
        )

        if mapped is None:
            return

        name = mapped.get(Index.USER_NAME, 'unknown')
        id = mapped.get(Index.USER_PLAYER_ID, 0)
        account_id = mapped.get(Index.USER_ACCOUNT_ID, 0)

        if return_abstract or not account_id:
            return ClassConverter.abstractuser_convert(
                dict(name=name, id=id, account_id=account_id), client=client
            )

        # ok if we should not return abstract, let's find all other parameters
        parameters = Params().create_new().put_definer('user', account_id).finish()

        resp = await http.request(
            Route.GET_USER_INFO, parameters, error_codes=codes
        )
        resp = Parser().with_split(':').should_map().parse(resp)

        resp.update(
            {k: mapped.get(k) for k in (Index.USER_NAME, Index.USER_ICON, Index.USER_ICON_TYPE)}
        )

        return ClassConverter.user_convert(resp, client)

    async def get_level(
        self, level_id: int = 0, timetuple: Tuple[int, int, int] = (0, -1, -1), *, client: Client
    ) -> Level:
        assert level_id >= -2

        type, number, cooldown = timetuple
        ext = {101: type, 102: number, 103: cooldown}

        codes = {
            -1: MissingAccess(message='Failed to get a level. Given ID: {}'.format(level_id))
        }

        parameters = Params().create_new().put_definer('levelid', level_id).finish()
        resp = await http.request(Route.DOWNLOAD_LEVEL, parameters, error_codes=codes)

        level_data = Parser().split('#').take(0).split(':').add_ext(ext).should_map().parse(resp)

        real_id = level_data.get(Index.LEVEL_ID)

        parameters = (
            Params().create_new().put_definer('search', real_id)
            .put_filters(Filters.setup_empty()).finish()
        )
        resp = await http.request(Route.LEVEL_SEARCH, parameters, error_codes=codes)
        resp = resp.split('#')

        # getting song
        song_data = resp[2]
        song = (
            Converter.to_normal_song(level_data.get(Index.LEVEL_AUDIO_TRACK)) if not song_data
            else ClassConverter.song_convert(Parser().with_split('~|~').should_map().parse(song_data))
        ).attach_client(client)

        # getting creator
        creator_data = resp[1]

        if not creator_data:
            id, name, account_id = (0, 'unknown', 0)
        else:
            id, name, account_id = creator_data.split(':')

        creator = ClassConverter.abstractuser_convert(
            dict(id=id, name=name, account_id=account_id), client=client
        )

        return ClassConverter.level_convert(
            level_data, song=song, creator=creator, client=client)

    async def get_timely(self, type: str = 'daily', *, client: Client) -> Level:
        w = ('daily', 'weekly').index(type)
        parameters = Params().create_new().put_weekly(w).finish()
        codes = {
            -1: MissingAccess(message='Failed to fetch a {!r} level.'.format(type))
        }
        resp = await http.request(Route.GET_TIMELY, parameters, error_codes=codes)

        if not resp:
            raise MissingAccess(message='Failed to fetch a {} level. Most likely it is being refreshed.'.format(type))

        num, cooldown, *_ = map(int, resp.split('|'))
        num %= 100000
        w += 1

        level = await self.get_level(-w, (w, num, cooldown), client=client)
        return level.attach_client(client)

    async def upload_level(
        self, data: str, name: str, level_id: int, version: int, length: int, audio_track: int,
        desc: str, song_id: int, is_auto: bool, original: int, two_player: bool, objects: int, coins: int,
        stars: int, unlisted: bool, ldm: bool, password: Optional[Union[int, str]],
        copyable: bool, *, load_after: bool, client: Client
    ) -> Level:
        data = Coder.zip(data)
        extra_string = ('_'.join(map(str, (0 for _ in range(55)))))
        desc = Coder.do_base64(desc)

        upload_seed = Coder.gen_level_upload_seed(data)
        seed2 = Coder.gen_chk(type='level', values=[upload_seed])
        seed = Coder.gen_rs()

        pwd = 0

        if copyable and password is None:
            pwd = 1

        check, add = str(password), 1000000

        if check.isdigit() and int(check) < add:
            pwd = add + int(password)

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('levelid', level_id).put_definer('song', song_id)
            .put_seed(seed).put_seed(seed2, suffix=2).put_seed(0, prefix='wt')
            .put_seed(0, prefix='wt', suffix=2).put_password(client.encodedpass)
            .put_username(client.name).finish()
        )

        payload = {
            'level_name': name, 'level_desc': desc, 'level_version': version,
            'level_length': length, 'audio_track': audio_track, 'auto': int(is_auto),
            'original': int(original), 'two_player': int(two_player), 'objects': objects,
            'coins': coins, 'requested_stars': stars, 'unlisted': int(unlisted), 'ldm': int(ldm),
            'password': pwd, 'level_string': data, 'extra_string': extra_string,
            'level_info': 'H4sIAAAAAAAAC_NIrVQoyUgtStVRCMpPSi0qUbDStwYAsgpl1RUAAAA='
        }

        payload_cased = {
            Converter.snake_to_camel(key): str(value) for key, value in payload.items()
        }

        parameters.update(payload_cased)

        level_id = await http.request(Route.UPLOAD_LEVEL, parameters)

        if level_id == -1:
            raise MissingAccess(message='Failed to upload a level.')

        elif load_after:
            return await client.get_level(level_id)

        else:
            from .classconverter import Level
            return Level(id=level_id).attach_client(client)

    async def get_user_list(self, type: int = 0, *, client: Client) -> List[AbstractUser]:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_type(type).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to get friends.'),
            -2: NothingFound('gd.AbstractUser')
        }

        resp = await http.request(Route.GET_USER_LIST, parameters, error_codes=codes)
        resp, parser = resp.split('|'), Parser().with_split(':').should_map()

        ret = []
        for elem in resp:
            temp = parser.parse(elem)

            parse_dict = {
                'name': temp[Index.USER_NAME],
                'id': temp[Index.USER_PLAYER_ID],
                'account_id': temp[Index.USER_ACCOUNT_ID]
            }

            ret.append(
                ClassConverter.abstractuser_convert(parse_dict, client=client)
            )

        return ret

    async def get_leaderboard(
        self, level: Level, strategy: LevelLeaderboardStrategy,
        *, client: Client
    ) -> List[LevelRecord]:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('levelid', level.id)
            .put_password(client.encodedpass).put_type(strategy.value).finish()
        )

        codes = {
            -1: MissingAccess(message='Failed to get leaderboard of the level: {!r}.'.format(level))
        }

        resp = await http.request(Route.GET_LEVEL_SCORES, parameters, error_codes=codes)

        if not resp:
            return list()

        resp, parser = resp.split('|'), Parser().with_split(':').add_ext({101: level.id}).should_map()

        res = list(
            ClassConverter.level_record_convert(parser.parse(data), strategy, client)
            for data in filter(_is_not_empty, resp)
        )

        return res

    async def get_top(
        self, strategy: LeaderboardStrategy,
        count: int, *, client: Client
    ) -> List[UserStats]:
        needs_login = (strategy.value in (1, 2))

        # special case: map 'players' -> 'top'
        strategy = strategy.name.lower() if strategy.value else 'top'

        params = Params().create_new().put_type(strategy).put_count(count)
        codes = {
            -1: MissingAccess(message='Failed to fetch leaderboard for strategy: {!r}.'.format(strategy))
        }

        if needs_login:
            check_logged_obj(client, 'get_top')
            params.put_definer('accountid', client.account_id).put_password(client.encodedpass)

        parameters = params.finish()

        resp = await http.request(Route.GET_USER_TOP, parameters, error_codes=codes)
        resp, parser = resp.split('|'), Parser().with_split(':').should_map()

        res = list(
            ClassConverter.user_stats_convert(parser.parse(data), client)
            for data in filter(_is_not_empty, resp)
        )

        return res

    async def login(self, client: Client, user: str, password: str) -> None:
        parameters = (
            Params().create_new().put_login_definer(username=user, password=password)
            .finish_login()
        )
        codes = {
            -1: LoginFailure(login=user, password=password)
        }

        resp = await http.request(Route.LOGIN, parameters, error_codes=codes)

        account_id, id, *junk = resp.split(',')

        prepared = {
            'name': user, 'password': password,
            'account_id': int(account_id), 'id': int(id)
        }
        for attr, value in prepared.items():
            client._upd(attr, value)

    async def load_save(self, client: Client) -> None:
        link = Route.GD_URL

        parameters = (
            Params().create_new().put_username(client.name).put_definer('password', client.password)
            .finish_login()
        )
        codes = {
            -11: MissingAccess(message='Failed to load data for client: {!r}.'.format(client))
        }

        resp = await http.request(Route.LOAD_DATA, parameters, error_codes=codes, custom_base=link)

        try:
            main, levels, *_ = resp.split(';')
            save_api = await api.save.from_string_async(main, levels, xor=False)
            save = await SaveParser.aio_parse(save_api.main.dump())
            client._upd('save_api', save_api)
            client._upd('save', save)

            return True

        except Exception:
            return False

    async def do_save(self, client: Client, data: str) -> None:
        link = Route.GD_URL

        codes = {
            -4: MissingAccess(message='Data too large.'),
            -5: MissingAccess(message='Invalid login credentials.'),
            -6: MissingAccess(message='Something wrong happened.')
        }

        parameters = (
            Params().create_new().put_username(client.name).put_definer('password', client.password)
            .put_save_data(data).finish_login()
        )

        resp = await http.request(Route.SAVE_DATA, parameters, custom_base=link, error_codes=codes)

        if resp != 1:
            raise MissingAccess('Failed to do backup for client: {!r}'.format(client))

    async def search_levels_on_page(
        self, page: int = 0, query: str = '', filters: Optional[Filters] = None, user: Optional[AbstractUser] = None,
        gauntlet: Optional[int] = None, *, raise_errors: bool = True, client: Client
    ) -> List[Level]:
        if filters is None:
            filters = Filters.setup_empty()

        params = (
            Params().create_new().put_definer('search', query)
            .put_page(page).put_total(0).put_filters(filters)
        )
        codes = {
            -1: MissingAccess(message='No levels were found.')
        }
        if filters.strategy == SearchStrategy.BY_USER:

            if user is None:
                check_logged_obj(client, 'search_levels_on_page(...)')

                id = client.id

                params.put_definer('accountid', client.account_id).put_password(client.encodedpass)
                params.put_local(1)

            else:
                id = user if isinstance(user, int) else user.id

            params.put_definer('search', id)  # override the 'str' parameter in request

        elif filters.strategy == SearchStrategy.FRIENDS:
            check_logged_obj(client, 'search_levels_on_page(..., client=client)')
            params.put_definer('accountid', client.account_id).put_password(client.encodedpass)

        if gauntlet is not None:
            params.put_definer('gauntlet', gauntlet)

        parameters = params.finish()

        resp = await http.request(
            Route.LEVEL_SEARCH, parameters, raise_errors=raise_errors,
            error_codes=codes)

        if not resp:
            return list()

        resp, parser = resp.split('#'), Parser().with_split('~|~').should_map()

        lvdata, cdata, sdata = resp[:3]

        songs = []
        for s in filter(_is_not_empty, sdata.split('~:~')):
            song = ClassConverter.song_convert(parser.parse(s))
            songs.append(song)

        creators = []
        for c in filter(_is_not_empty, cdata.split('|')):
            creator = ClassConverter.abstractuser_convert(
                dict(zip(('id', 'name', 'account_id'), c.split(':'))), client=client
            )
            creators.append(creator)

        levels = []
        parser.with_split(':').add_ext({101: 0, 102: -1, 103: -1})

        for lv in filter(_is_not_empty, lvdata.split('|')):
            data = parser.parse(lv)

            song_id = data.get(Index.LEVEL_SONG_ID)
            song = Converter.to_normal_song(
                data.get(Index.LEVEL_AUDIO_TRACK)
            ) if not song_id else utils.get(songs, id=song_id)

            creator_id = data.get(Index.LEVEL_CREATOR_ID)
            creator = utils.get(creators, id=creator_id)
            if creator is None:
                creator = ClassConverter.abstractuser_convert(
                    dict(id=creator_id, name='unknown', account_id=0), client=client
                )

            levels.append(ClassConverter.level_convert(data, song, creator, client))

        return levels

    async def search_levels(
        self, query: str = '', filters: Optional[Filters] = None, user: Optional[AbstractUser] = None,
        pages: Optional[Sequence[int]] = None, *, client: Client
    ) -> List[Level]:
        to_run = [
            self.search_levels_on_page(
                query=query, filters=filters, user=user, page=page, raise_errors=False, client=client
            ) for page in pages
        ]

        return await self.run_many(to_run)

    async def report_level(self, level: Level) -> None:
        parameters = Params().create_new('web').put_definer('levelid', level.id).finish()
        codes = {
            -1: MissingAccess(message='Failed to report a level: {!r}.'.format(level))
        }

        await http.request(Route.REPORT_LEVEL, parameters, error_codes=codes)

    async def delete_level(self, level: Level, *, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('levelid', level.id).put_password(client.encodedpass).finish_level()
        )

        resp = await http.request(Route.DELETE_LEVEL, parameters)

        if resp != 1:
            raise MissingAccess(message='Failed to delete a level: {}.'.format(level))

        # update level's is_alive coroutine to return False only.
        async def is_alive(*args) -> bool:
            return False

        level.is_alive = is_alive

    async def update_level_desc(self, level: Level, content: str, *, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('levelid', level.id)
            .put_level_desc(content).finish()
        )

        resp = await http.request(Route.UPDATE_LEVEL_DESC, parameters)

        if resp != 1:
            raise MissingAccess(message='Failed to update description of the level: {}.'.format(level))

        # update level's description on success
        level.options.edit(description=content)

    async def rate_level(self, level: Level, rating: int, *, client: Client) -> None:
        assert 0 < rating <= 10, 'Invalid star value given.'

        rs = Coder.gen_rs()
        values = [level.id, rating, rs, client.account_id, 0, 0]
        chk = Coder.gen_chk(type='like_rate', values=values)

        parameters = (
            Params().create_new().put_definer('levelid', level.id)
            .put_definer('accountid', client.account_id).put_password(client.encodedpass)
            .put_udid(0).put_uuid(0).put_definer('stars', rating).put_rs(rs).put_chk(chk).finish()
        )

        resp = await http.request(Route.RATE_LEVEL_STARS, parameters)

        if resp != 1:
            raise MissingAccess(message='Failed to rate level: {}.'.format(level))

    async def rate_demon(
        self, level: Level, demon_rating: DemonDifficulty,
        mod: bool, *, client: Client
    ) -> None:
        rating_level = demon_rating.value

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('levelid', level.id)
            .put_definer('rating', rating_level).put_mode(int(mod)).finish_mod()
        )
        codes = {
            -2: MissingAccess(message='Attempt to rate as mod without mod permissions.')
        }

        resp = await http.request(Route.RATE_LEVEL_DEMON, parameters, error_codes=codes)

        if not resp:
            return False
        elif isinstance(resp, int) and resp > 0:
            return True

    async def send_level(self, level: Level, rating: int, featured: bool, *, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('levelid', level.id)
            .put_definer('stars', rating).put_feature(int(featured)).finish_mod()
        )
        codes = {
            -2: MissingAccess(message='Missing moderator permissions to send a level: {!r}.'.format(level))
        }

        resp = await http.request(Route.SUGGEST_LEVEL_STARS, parameters, error_codes=codes)

        if resp != 1:
            raise MissingAccess(message='Failed to send a level: {!r}.'.format(level))

    async def like(self, item: Union[Comment, Level], dislike: bool = False, *, client: Client) -> None:
        if hasattr(item, 'is_featured'):  # level
            typeid = 1
            special = 0

        elif hasattr(item, 'is_spam'):  # comment
            if not item.type.value:  # level comment
                typeid = 2
                special = item.level_id

            else:  # profile comment
                typeid = 3
                special = item.id

        else:  # wrong type?!
            return

        like = dislike ^ 1

        rs = Coder.gen_rs()
        values = [special, item.id, like, typeid, rs, client.account_id, 0, 0]
        chk = Coder.gen_chk(type='like_rate', values=values)

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_udid(0).put_uuid(0)
            .put_definer('itemid', item.id).put_like(like).put_type(typeid)
            .put_special(special).put_rs(rs).put_chk(chk).finish()
        )

        resp = await http.request(Route.LIKE_ITEM, parameters)

        if resp != 1:
            raise MissingAccess(message='Failed to like an item: {}.'.format(item))

    async def get_page_messages(
        self, sent_or_inbox: str, page: int, *, raise_errors: bool = True, client: Client
    ) -> List[Message]:
        assert sent_or_inbox in ('inbox', 'sent')
        inbox = 0 if sent_or_inbox != 'sent' else 1

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_page(page).put_total(0).get_sent(inbox).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to get messages.'),
            -2: NothingFound('gd.Message')
        }

        resp = await http.request(
            Route.GET_PRIVATE_MESSAGES, parameters, error_codes=codes,
            raise_errors=raise_errors
        )
        resp = Parser().split('#').take(0).check_empty().split('|').parse(resp)

        if resp is None:
            return list()

        parser = Parser().with_split(':').should_map()
        res = list(
            ClassConverter.message_convert(
                parser.parse(elem), client.get_parse_dict(), client
            ) for elem in resp
        )

        return res

    async def get_messages(
        self, sent_or_inbox: str, pages: Optional[Sequence[int]] = None,
        *, client: Client
    ) -> List[Message]:
        assert sent_or_inbox in ('inbox', 'sent')

        to_run = [
            self.get_page_messages(
                sent_or_inbox=sent_or_inbox, page=page, raise_errors=False, client=client
            ) for page in pages
        ]

        return await self.run_many(to_run)

    async def post_comment(self, content: str, *, client: Client) -> None:
        to_gen = [client.name, 0, 0, 1]

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_username(client.name).put_password(client.encodedpass)
            .put_comment(content, to_gen).comment_for('profile').finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to post a comment.')
        }

        await http.request(Route.UPLOAD_ACC_COMMENT, parameters, error_codes=codes)

    async def comment_level(
        self, level: Level, content: str,
        percentage: int, *, client: Client
    ) -> None:
        assert percentage <= 100, '{}% > 100% percentage arg was recieved.'.format(percentage)

        percentage = round(percentage)  # just in case
        to_gen = [client.name, level.id, percentage, 0]

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_username(client.name).put_password(client.encodedpass)
            .put_comment(content, to_gen).comment_for('level', level.id)
            .put_percent(percentage).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to post a comment on a level: {!r}.'.format(level))
        }

        await http.request(Route.UPLOAD_COMMENT, parameters, error_codes=codes)

    async def delete_comment(self, comment: Comment, *, client: Client) -> None:
        cases = {
            0: Route.DELETE_LEVEL_COMMENT,
            1: Route.DELETE_ACC_COMMENT
        }
        route = cases.get(comment.type.value)
        parameters = (
            Params().create_new().put_definer('commentid', comment.id)
            .put_definer('accountid', client.account_id).put_password(client.encodedpass)
            .comment_for(comment.type.name.lower(), comment.level_id).finish()
        )
        resp = await http.request(route, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to delete a comment: {!r}.'.format(comment))

    async def send_friend_request(self, target: AbstractUser, message: str, client: Client) -> None:
        if message is None:
            message = ''

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_recipient(target.account_id).put_fr_comment(message)
            .put_password(client.encodedpass).finish()
        )
        resp = await http.request(Route.SEND_REQUEST, parameters)

        if not resp:  # if request is already sent
            return

        elif resp != 1:
            raise MissingAccess(message='Failed to send a friend request to user: {!r}.'.format(target))

    async def delete_friend_req(self, req: FriendRequest, client: Client) -> None:
        user = req.author if not req.type.value else req.recipient
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('user', user.account_id).put_password(client.encodedpass)
            .put_is_sender(req.type.name.lower()).finish()
        )
        resp = await http.request(Route.DELETE_REQUEST, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to delete a friend request: {!r}.'.format(req))

    async def accept_friend_req(self, req: FriendRequest, client: Client) -> None:
        if req.type.value:  # is gd.MessageOrRequestType.SENT
            raise MissingAccess(
                message='Failed to accept a friend request. Reason: request is sent, not recieved one.'
            )
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('user', req.author.account_id)
            .put_definer('requestid', req.id).finish()
        )
        resp = await http.request(Route.ACCEPT_REQUEST, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to accept a friend request: {!r}.'.format(req))

    async def read_friend_req(self, req: FriendRequest, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('requestid', req.id).finish()
        )
        resp = await http.request(Route.READ_REQUEST, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to read a friend request: {!r}.'.format(req))
        req.options.update({'is_read': True})

    async def read_message(self, msg: Message, client: Client) -> str:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('messageid', msg.id).put_is_sender(msg.type.name.lower())
            .put_password(client.encodedpass).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to read a message: {!r}.'.format(msg))
        }
        resp = await http.request(
            Route.READ_PRIVATE_MESSAGE, parameters, error_codes=codes,
        )
        resp = Parser().with_split(':').should_map()

        ret = Coder.decode(
            type='message', string=resp.get(Index.MESSAGE_BODY)
        )
        msg._body = ret
        return ret

    async def delete_message(self, msg: Message, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_definer('messageid', msg.id).put_password(client.encodedpass)
            .put_is_sender(msg.type.name.lower()).finish()
        )
        resp = await http.request(Route.DELETE_PRIVATE_MESSAGE, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to delete a message: {!r}.'.format(msg))

    async def get_gauntlets(self, *, client: Client) -> List[Gauntlet]:
        parameters = Params().create_new().finish()

        resp = await http.request(Route.GET_GAUNTLETS, parameters)

        resp = Parser().split('#').take(0).split('|').parse(resp)

        parser = Parser().with_split(':').should_map()
        res = list(
            ClassConverter.gauntlet_convert(parser.parse(gdata), client)
            for gdata in filter(_is_not_empty, resp)
        )

        return res

    async def get_page_map_packs(
        self, page: int = 0, *, raise_errors: bool = True,
        client: Client
    ) -> List[MapPack]:
        parameters = Params().create_new().put_page(page).finish()

        resp = await http.request(Route.GET_MAP_PACKS, parameters)

        resp = Parser().split('#').take(0).split('|').check_empty().should_map().parse(resp)

        if resp is None:
            if raise_errors:
                raise NothingFound('gd.MapPack')
            return list()

        parser = Parser().with_split(':').should_map()

        res = list(ClassConverter.map_pack_convert(parser.parse(elem), client) for elem in resp)
        return res

    async def get_map_packs(self, pages: Sequence[int], *, client: Client) -> List[MapPack]:
        to_run = [
            self.get_page_map_packs(
                page=page, raise_errors=False, client=client
            ) for page in pages
        ]

        return await self.run_many(to_run)

    async def get_page_friend_requests(
        self, sent_or_inbox: str = 'inbox', page: int = 0,
        *, raise_errors: bool = True, client: Client
    ) -> List[FriendRequest]:
        inbox = int(sent_or_inbox == 'sent')

        parameters = (
            Params().create_new().put_definer('accountid', str(client.account_id))
            .put_password(client.encodedpass).put_page(page).put_total(0).get_sent(inbox).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to get friend requests on page {}.'.format(page)),
            -2: NothingFound('gd.FriendRequest')
        }

        resp = await http.request(
            Route.GET_FRIEND_REQUESTS, parameters, error_codes=codes, raise_errors=raise_errors
        )
        resp = Parser().split('#').take(0).split('|').check_empty().parse(resp)

        if resp is None:
            return list()

        parser = Parser().split(':').add_ext({101: inbox}).should_map()
        res = list(
            ClassConverter.request_convert(
                parser.parse(elem), client.get_parse_dict(), client
            ) for elem in resp
        )

        return res

    async def get_friend_requests(
        self, pages: Sequence[int], sent_or_inbox: str = 'inbox', *, client: Client
    ) -> List[FriendRequest]:
        assert sent_or_inbox in ('sent', 'inbox')

        to_run = [
            self.get_page_friend_requests(
                sent_or_inbox=sent_or_inbox, page=page, raise_errors=False, client=client
            ) for page in pages
        ]

        return await self.run_many(to_run)

    async def retrieve_page_comments(
        self, user: AbstractUser, type: str = 'profile', page: int = 0, *,
        raise_errors: bool = True, strategy: CommentStrategy, client: Client
    ) -> List[Comment]:
        assert isinstance(page, int) and page >= 0
        assert type in ('profile', 'level')

        is_level = (type == 'level')

        typeid = is_level ^ 1
        definer = 'userid' if is_level else 'accountid'
        selfid = user.id if is_level else user.account_id
        route = Route.GET_COMMENT_HISTORY if is_level else Route.GET_ACC_COMMENTS

        parser = Parser().add_ext({101: typeid}).should_map()

        if is_level:
            parser.split(':').take(0).split('~')
        else:
            parser.with_split('~')

        param_obj = Params().create_new().put_definer(definer, selfid).put_page(page).put_total(0)
        if is_level:
            param_obj.put_mode(strategy.value)
        parameters = param_obj.finish()

        codes = {
            -1: MissingAccess(message='Failed to retrieve comment for user: {!r}.'.format(user))
        }

        resp = await http.request(route, parameters, error_codes=codes, raise_errors=raise_errors)

        if not resp:
            return list()

        resp = resp.split('#').pop(0)

        if not resp:
            if raise_errors:
                raise NothingFound('gd.Comment')
            return list()

        res = list(
            ClassConverter.comment_convert(
                parser.parse(elem), user._dict_for_parse, client
            ) for elem in resp.split('|')
        )

        return res

    async def retrieve_comments(
        self, user: AbstractUser, pages: Sequence[int], type: str = 'profile',
        *, strategy: CommentStrategy, client: Client
    ) -> List[Comment]:
        assert type in ('profile', 'level')

        to_run = [
            self.retrieve_page_comments(
                type=type, user=user, page=page, raise_errors=False, strategy=strategy, client=client
            ) for page in pages
        ]

        return await self.run_many(to_run)

    async def get_level_comments(
        self, level: Level, strategy: CommentStrategy,
        amount: int, client: Client
    ) -> List[Comment]:
        st_value = strategy.value

        parameters = (
            Params().create_new().put_definer('levelid', level.id).put_page(0)
            .put_total(0).put_mode(st_value).put_count(amount).finish()
        )
        codes = {
            -1: MissingAccess(message='Failed to get comments of a level: {!r}.'.format(level)),
            -2: NothingFound('gd.Comment')
        }

        resp = await http.request(Route.GET_COMMENTS, parameters, error_codes=codes)

        resp = Parser().split('#').take(0).split('|').parse(resp)
        parser = Parser().with_split('~').should_map()

        res = []
        for elem in resp:
            com_data, user_data = (parser.parse(part) for part in elem.split(':'))
            com_data.update({1: level.id, 101: 0, 102: 0})

            user_dict = {
                'account_id': user_data[Index.USER_ACCOUNT_ID],
                'id': com_data[Index.COMMENT_AUTHOR_ID],
                'name': user_data[Index.USER_NAME]
            }

            res.append(ClassConverter.comment_convert(com_data, user_dict, client))

        return res

    async def block_user(self, user: AbstractUser, unblock: bool = False, *, client: Client) -> None:
        route = Route.UNBLOCK_USER if unblock else Route.BLOCK_USER
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass)
            .put_definer('user', user.account_id).finish()
        )
        resp = await http.request(route, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to (un)block a user: {!r}.'.format(user))

    async def unfriend_user(self, user: AbstractUser, *, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_definer('user', user.account_id).finish()
        )
        resp = await http.request(Route.REMOVE_FRIEND, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to unfriend a user: {!r}.'.format(user))

    async def send_message(self, target: AbstractUser, subject: str, body: str, *, client: Client) -> None:
        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_message(subject, body).put_recipient(target.account_id)
            .put_password(client.encodedpass).finish()
        )
        resp = await http.request(Route.SEND_PRIVATE_MESSAGE, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to send a message to a user: {!r}.'.format(target))

    async def update_profile(self, settings: Dict[str, int], *, client: Client) -> None:
        settings_cased = {Converter.snake_to_camel(name): value for name, value in settings.items()}

        rs = Coder.gen_rs()

        req_chk_params = [client.account_id]
        for param in (
            'user_coins', 'demons', 'stars', 'coins', 'icon_type',
            'icon', 'diamonds', 'acc_icon', 'acc_ship', 'acc_ball',
            'acc_bird', 'acc_dart', 'acc_robot', 'acc_glow',
            'acc_spider', 'acc_explosion'
        ):
            req_chk_params.append(settings[param])

        chk = Coder.gen_chk(type='userscore', values=req_chk_params)

        parameters = (
            Params().create_new().put_definer('accountid', client.account_id)
            .put_password(client.encodedpass).put_username(client.name)
            .put_seed(rs).put_seed(chk, suffix=str(2)).finish()
        )

        parameters.update(settings_cased)

        resp = await http.request(Route.UPDATE_USER_SCORE, parameters)

        if not resp > 0:
            raise MissingAccess(message='Failed to update profile of a client: {!r}'.format(client))

    async def generate_icon(self, form: str, id: int, color_1: int, color_2: int, has_glow: bool) -> bytes:
        # fetch an icon from gdbrowser site
        query = {
            'form': form,
            'icon': id,
            'col1': color_1,
            'col2': color_2,
            'glow': int(has_glow),
            'noUser': int(True)
        }
        endpoint = 'https://gdbrowser.com/icon/icon'
        method = 'GET'

        response = await http.normal_request(url=endpoint, params=query, method=method)
        return await response.read()

    async def update_settings(
        self, msg: int, friend_req: int, comments: int,
        youtube: str, twitter: str, twitch: str, *, client: Client
    ) -> None:
        parameters = (
            Params().create_new('web').put_definer('accountid', client.account_id)
            .put_password(client.encodedpass)
            .put_profile_upd(msg, friend_req, comments, youtube, twitter, twitch).finish_login()
        )
        resp = await http.request(Route.UPDATE_ACC_SETTINGS, parameters)
        if resp != 1:
            raise MissingAccess(message='Failed to update profile settings of a client: {!r}.'.format(client))

    async def run_many(self, tasks: List[asyncio.Task]) -> Any:
        res = await asyncio.gather(*tasks)

        res = [elem for elem in res if elem]

        if all(_iterable(elem) for elem in res):
            res = list(chain.from_iterable(res))

        return res


def _iterable(maybe_iterable: Iterable) -> bool:
    try:
        iter(maybe_iterable)
        return True
    except Exception:
        return False


def _is_not_empty(sequence: Sequence) -> bool:
    return bool(len(sequence))


_session = GDSession()
